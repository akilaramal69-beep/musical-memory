import asyncio
import os
import re
import time
import urllib.parse
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from plugins.config import Config
from plugins.admin import admin_only
from plugins.helper.database import is_premium_user
from plugins.helper.extractor import extract_links
from plugins.helper.upload import download_url, upload_file, humanbytes
from playwright.async_api import async_playwright

# Store active scraper tasks: {user_id: {"task": Task, "stop": bool, "count": int}}
ACTIVE_SCRAPERS = {}

async def update_scraper_status(message: Message, text: str):
    """Update the status message, avoiding flood errors."""
    try:
        await message.edit_text(text)
    except Exception:
        pass

@Client.on_message(filters.command("scrape") & filters.private)
@admin_only
async def scrape_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    if user_id in ACTIVE_SCRAPERS:
        return await message.reply_text("⚠️ You already have an active scraper running. Use `/stop_scrape` first.")
    
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/scrape <url>`\nProvide a category or gallery URL.")
    
    url = args[1].strip()
    if not url.startswith(("http://", "https://")):
        return await message.reply_text("❌ Invalid URL.")

    # Start the scraping process in the background
    status_msg = await message.reply_text("🔍 **Initializing Scraper...**")
    
    stop_event = asyncio.Event()
    ACTIVE_SCRAPERS[user_id] = {"stop_event": stop_event, "url": url}
    
    asyncio.create_task(run_scraper(client, status_msg, user_id, url, stop_event))

@Client.on_message(filters.command("stop_scrape") & filters.private)
@admin_only
async def stop_scrape_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in ACTIVE_SCRAPERS:
        return await message.reply_text("❌ No active scraper found.")
    
    ACTIVE_SCRAPERS[user_id]["stop_event"].set()
    await message.reply_text("🛑 Stopping scraper... Please wait for the current upload to finish.")

async def run_scraper(client: Client, status_msg: Message, user_id: int, start_url: str, stop_event: asyncio.Event):
    try:
        await update_scraper_status(status_msg, f"🌐 **Searching for videos on:**\n`{start_url}`")
        
        video_links = await discover_video_links(start_url)
        if not video_links:
            await update_scraper_status(status_msg, "❌ No video links found on the provided page.")
            ACTIVE_SCRAPERS.pop(user_id, None)
            return

        total = len(video_links)
        await update_scraper_status(status_msg, f"✅ Found **{total}** potential videos. Starting sequential upload...")
        
        for i, link_data in enumerate(video_links, 1):
            if stop_event.is_set():
                await update_scraper_status(status_msg, f"🛑 **Scraper stopped.**\nProcessed: {i-1}/{total}")
                break
            
            video_page_url = link_data["url"]
            title = link_data["title"] or f"Video_{i}"
            thumb_url = link_data["thumb"]
            
            try:
                await update_scraper_status(status_msg, f"🔥 **Processing Video {i}/{total}**\n\n📄 **Title:** `{title}`\n🔗 **Page:** {video_page_url}")
                
                # 1. Extract direct media URL
                # We use use_browser=True for maximum compatibility
                result = await extract_links(video_page_url, use_browser=True, timeout=30)
                best_link = result.get("best_link")
                
                if not best_link:
                    Config.LOGGER.warning(f"Could not extract links for: {video_page_url}")
                    continue
                
                # 2. Download Media
                start_time = [time.time()]
                clean_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:80]
                filename = f"{clean_title}.mp4"
                
                file_path, mime = await download_url(
                    best_link, filename, status_msg, start_time, user_id
                )
                
                if not file_path or not os.path.exists(file_path):
                    continue

                # 3. Handle Thumbnail if available
                local_thumb = None
                if thumb_url and thumb_url.startswith("http"):
                    try:
                        thumb_path = os.path.join(Config.DOWNLOAD_LOCATION, f"thumb_{user_id}_{int(time.time())}.jpg")
                        from utils.shared import get_http_session
                        session = await get_http_session()
                        async with session.get(thumb_url, timeout=10) as resp:
                            if resp.status == 200:
                                with open(thumb_path, "wb") as f:
                                    f.write(await resp.read())
                                local_thumb = thumb_path
                    except Exception as te:
                        Config.LOGGER.warning(f"Failed to download thumb {thumb_url}: {te}")

                # 4. Upload
                await update_scraper_status(status_msg, f"📤 **Uploading {i}/{total}...**\n`{filename}`")
                
                await upload_file(
                    client, status_msg.chat.id, file_path, mime,
                    caption=title, thumb=local_thumb, progress_msg=status_msg, 
                    start_time_ref=start_time, user_id=user_id
                )
                
                # Clean up
                if file_path and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass
                if local_thumb and os.path.exists(local_thumb):
                    try: os.remove(local_thumb)
                    except: pass
                    
            except Exception as e:
                Config.LOGGER.error(f"Error processing scraped video {i}: {e}")
                await status_msg.reply_text(f"⚠️ Error on video {i}: `{e}`")
                continue

        await status_msg.reply_text(f"🏁 **Scraping finished!**\nTotal processed: {total}")
        
    except Exception as e:
        Config.LOGGER.exception("Scraper fatal error")
        await status_msg.reply_text(f"❌ Scraper crashed: `{e}`")
    finally:
        ACTIVE_SCRAPERS.pop(user_id, None)

async def discover_video_links(url: str) -> list[dict]:
    """Use Playwright to find all video page links and their titles/thumbs."""
    links = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait a bit for JS content
            await asyncio.sleep(5)
            # Scroll down to trigger lazy loading
            await page.evaluate("window.scrollBy(0, 2000)")
            await asyncio.sleep(2)
            
            # Extract links
            items = await page.evaluate(r"""() => {
                const results = [];
                const seen = new Set();
                
                // Generic detection for video cards
                // Look for <a> tags that contain an <img> and point to a /video/ or /view/ or /idX path
                const anchors = document.querySelectorAll('a');
                anchors.forEach(a => {
                    const href = a.href;
                    if (!href || seen.has(href)) return;
                    
                    // Basic filters to identify video page links
                    const isVideoLink = /video|view|watch|v=|id=\d+/.test(href.toLowerCase());
                    if (!isVideoLink) return;
                    
                    const img = a.querySelector('img');
                    if (img) {
                        seen.add(href);
                        results.push({
                            url: href,
                            title: img.alt || a.innerText || img.title || "",
                            thumb: img.src || img.dataset.src || ""
                        });
                    }
                });
                
                // Try specific selectors for common sites if generic fails
                if (results.length === 0) {
                     // XVideos / Pornhub style
                     document.querySelectorAll('.thumb-block, .ph-video-block, .item, .video-thumb').forEach(el => {
                         const a = el.querySelector('a');
                         const img = el.querySelector('img');
                         if (a && img && !seen.has(a.href)) {
                             seen.add(a.href);
                             results.push({
                                 url: a.href,
                                 title: a.title || img.alt || a.innerText || "",
                                 thumb: img.src || img.dataset.src || ""
                             });
                         }
                     });
                }
                
                return results;
            }""")
            
            # Fallback if evaluate results is empty due to syntax or something
            if not items:
                # Try simple python-side extraction
                extracted = await page.query_selector_all("a")
                for a in extracted:
                    href = await a.get_attribute("href")
                    if not href or not href.startswith("http"): continue
                    if "/video" in href or "/view" in href:
                        img = await a.query_selector("img")
                        title = ""
                        thumb = ""
                        if img:
                            title = await img.get_attribute("alt") or ""
                            thumb = await img.get_attribute("src") or ""
                        else:
                            title = await a.inner_text()
                        
                        links.append({"url": href, "title": title.strip(), "thumb": thumb})

            else:
                links = items

        except Exception as e:
            Config.LOGGER.error(f"Discovery error: {e}")
        finally:
            await browser.close()
            
    # Filter out duplicates and invalid links
    unique = []
    seen_urls = set()
    for l in links:
        if l["url"] not in seen_urls and len(l["url"]) > 10:
            seen_urls.add(l["url"])
            unique.append(l)
            
    return unique
