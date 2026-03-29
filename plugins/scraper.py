import asyncio
import os
import re
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from plugins.config import Config
from plugins.admin import admin_only
from plugins.commands import _do_upload_logic
from playwright.async_api import async_playwright

# Store active scraper tasks: {user_id: {"stop_event": Event}}
ACTIVE_SCRAPERS = {}

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

    status_msg = await message.reply_text("🔍 **Initializing Scraper...**")
    
    stop_event = asyncio.Event()
    ACTIVE_SCRAPERS[user_id] = {"stop_event": stop_event}
    
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
        await status_msg.edit_text(f"🌐 **Searching for videos on:**\n`{start_url}`")
        
        video_links = await discover_video_links(start_url)
        if not video_links:
            await status_msg.edit_text("❌ No video links found on the provided page.")
            ACTIVE_SCRAPERS.pop(user_id, None)
            return

        total = len(video_links)
        await status_msg.edit_text(f"✅ Found **{total}** videos. Starting sequential uploads using standard logic...")
        
        # We'll use a single cancellation ref for the entire scraper run
        cancel_ref = [False]
        
        for i, link_data in enumerate(video_links, 1):
            if stop_event.is_set():
                await status_msg.reply_text(f"🛑 **Scraper stopped.** processed {i-1}/{total}")
                break
            
            video_url = link_data["url"]
            # To match "normal upload logic", we send a fresh message for each video's progress
            upload_status = await client.send_message(user_id, f"📥 **Scraper [{i}/{total}]:** Preparing...")
            
            try:
                # Use the EXACT same logic as the normal /upload or link detection
                await _do_upload_logic(
                    client=client,
                    reply_to=upload_status,
                    user_id=user_id,
                    url=video_url,
                    filename=None,        # Auto-detect title
                    cancel_ref=cancel_ref,
                    force_document=False, # Default to media
                    format_id=None,       # Best quality
                )
                # Cleanup the per-video status message if it was successful (optional)
                # await upload_status.delete() 
            except Exception as e:
                Config.LOGGER.error(f"Scraper error on video {i}: {e}")
                try: await upload_status.edit_text(f"❌ **Scraper Error {i}/{total}:**\n`{e}`")
                except: pass
            
            # Small delay between tasks to prevent flood
            await asyncio.sleep(2)

        await status_msg.reply_text(f"🏁 **Scraping finished!**\nTotal processed: {total}")
        
    except Exception as e:
        Config.LOGGER.exception("Scraper fatal error")
        await status_msg.reply_text(f"❌ Scraper crashed: `{e}`")
    finally:
        ACTIVE_SCRAPERS.pop(user_id, None)

async def discover_video_links(url: str) -> list[dict]:
    """Use Playwright to find all video page links."""
    links = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        try:
            # Go with a long timeout for slow sites
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)
            # Scroll to load lazy content
            await page.evaluate("window.scrollBy(0, 3000)")
            await asyncio.sleep(2)
            
            # Simple link extraction: look for <a> tags with video-like hrefs
            items = await page.evaluate(r"""() => {
                const results = [];
                const seen = new Set();
                const anchors = document.querySelectorAll('a');
                
                // Common adult site filters
                const whitelist = [/video\//, /view\//, /watch\//, /id=\d+/, /movies\//, /v\//];
                const blacklist = [/login/, /signup/, /forgot/, /upload/, /support/, /terms/, /privacy/];

                anchors.forEach(a => {
                    const href = a.href;
                    if (!href || seen.has(href)) return;
                    if (blacklist.some(r => r.test(href.toLowerCase()))) return;
                    
                    if (whitelist.some(r => r.test(href.toLowerCase()))) {
                        seen.add(href);
                        results.push({ url: href });
                    }
                });
                return results;
            }""")
            links = items
        except Exception as e:
            Config.LOGGER.error(f"Scraper discovery failed: {e}")
        finally:
            await browser.close()
            
    return links
