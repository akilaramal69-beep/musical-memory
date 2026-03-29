import asyncio
import functools
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from plugins.config import Config
from plugins.helper.database import (
    get_all_users, total_users_count, ban_user, unban_user, is_banned,
    is_premium_user, set_premium_user, get_user_stats, add_user
)
from plugins.helper.upload import humanbytes
from plugins.helper.site_scraper import scrape_category_links
import psutil
import os


SCRAPE_QUEUE = {}
SCRAPE_TASKS = {}


def admin_only(func):
    """Decorator: only owner or admins can run this."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != Config.OWNER_ID and user_id not in Config.ADMIN:
            return await message.reply_text("🚫 Admin only command.", quote=True)
        return await func(client, message)
    return wrapper


# ── /total ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("total") & filters.private)
@admin_only
async def total_users(client: Client, message: Message):
    count = await total_users_count()
    await message.reply_text(f"👥 **Total registered users:** `{count}`", quote=True)


# ── /statusall (admin status) ────────────────────────────────────────────────

@Client.on_message(filters.command("statusall") & filters.private)
@admin_only
async def statusall_handler(client: Client, message: Message):
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("./")
    count = await total_users_count()

    from plugins.helper.upload import check_ffmpeg
    ffmpeg_found = await check_ffmpeg()

    text = (
        "🚀 **Bot Status**\n\n"
        f"🖥 **CPU:** {cpu}%\n"
        f"🧠 **RAM:** {humanbytes(ram.used)} / {humanbytes(ram.total)} ({ram.percent}%)\n"
        f"💽 **Disk:** {humanbytes(disk.used)} / {humanbytes(disk.total)} ({disk.percent}%)\n"
        f"👥 **Users:** {count}\n"
        f"🎥 **FFmpeg:** {'✅ Found' if ffmpeg_found else '❌ Not Found'}"
    )
    await message.reply_text(text, quote=True)


# ── /broadcast ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("broadcast") & filters.private)
@admin_only
async def broadcast_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2 and not message.reply_to_message:
        return await message.reply_text("Usage: `/broadcast <message>` or reply to a message with /broadcast", quote=True)

    broadcast_text = (
        " ".join(args[1:]) if len(args) > 1
        else message.reply_to_message.text or message.reply_to_message.caption or ""
    )
    if not broadcast_text:
        return await message.reply_text("❌ Nothing to broadcast.", quote=True)

    users = await get_all_users()
    sent, failed = 0, 0
    status = await message.reply_text(f"📢 Broadcasting to **{len(users)}** users…", quote=True)

    for user in users:
        try:
            await client.send_message(user["_id"], broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ **Broadcast complete!**\n\n✔️ Sent: `{sent}`\n❌ Failed: `{failed}`"
    )


# ── /ban ──────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ban") & filters.private)
@admin_only
async def ban_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/ban <user_id>`", quote=True)
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    await ban_user(target)
    await message.reply_text(f"⛔ User `{target}` has been banned.", quote=True)


# ── /unban ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("unban") & filters.private)
@admin_only
async def unban_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/unban <user_id>`", quote=True)
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    await unban_user(target)
    await message.reply_text(f"✅ User `{target}` has been unbanned.", quote=True)


# ── /premium ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("premium") & filters.private)
@admin_only
async def premium_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "Usage:\n"
            "`/premium <user_id>` - Check premium status\n"
            "`/premium <user_id> on` - Enable premium\n"
            "`/premium <user_id> off` - Disable premium",
            quote=True
        )
    
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    
    if len(args) == 2:
        is_prem = await is_premium_user(target)
        status = "⭐ **Premium**" if is_prem else "👤 **Free**"
        await message.reply_text(f"User `{target}` status: {status}", quote=True)
    elif args[2].lower() == "on":
        await add_user(target)  # Ensure user document is initialized to prevent bugs
        await set_premium_user(target, True)
        await message.reply_text(f"✅ User `{target}` is now **⭐ Premium**!", quote=True)
    elif args[2].lower() == "off":
        await add_user(target)
        await set_premium_user(target, False)
        await message.reply_text(f"✅ User `{target}` is now **👤 Free**.", quote=True)
    else:
        await message.reply_text("Usage: `/premium <user_id> on/off`", quote=True)


@Client.on_message(filters.command("scrape") & filters.private)
@admin_only
async def scrape_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "🔍 **Site Scraper**\n\n"
            "Usage: `/scrape <url> [max_videos]`\n\n"
            "Example: `/scrape https://example.com/videos 20`\n\n"
            "This will scrape all video links from the page and upload them one by one.\n"
            "Send /scrape_stop to cancel.",
            quote=True
        )
    
    url = args[1].strip()
    if not url.startswith(("http://", "https://")):
        return await message.reply_text("❌ Invalid URL. Must start with http:// or https://", quote=True)
    
    try:
        max_videos = int(args[2]) if len(args) > 2 else 50
        max_videos = min(max_videos, 100)
    except ValueError:
        max_videos = 50
    
    user_id = message.from_user.id
    
    if user_id in SCRAPE_QUEUE:
        return await message.reply_text(
            "⚠️ A scraping job is already running!\n"
            "Use /scrape_stop to cancel the current job.",
            quote=True
        )
    
    status_msg = await message.reply_text(
        f"🔍 **Scraping site…**\n\n"
        f"📎 URL: `{url}`\n"
        f"🎬 Max videos: {max_videos}\n\n"
        "Please wait…",
        quote=True
    )
    
    SCRAPE_QUEUE[user_id] = {
        "status_msg_id": status_msg.id,
        "total": 0,
        "done": 0,
        "failed": 0,
        "cancel": False,
        "active": False
    }
    
    try:
        task = asyncio.create_task(_scrape_and_upload(client, message.chat.id, user_id, url, max_videos, status_msg))
        SCRAPE_TASKS[user_id] = task
    except Exception as e:
        Config.LOGGER.error(f"Failed to start scrape task: {e}")
        await status_msg.edit_text(f"❌ Failed to start: {e}")
        SCRAPE_QUEUE.pop(user_id, None)


async def _scrape_and_upload(client: Client, chat_id: int, user_id: int, url: str, max_videos: int, status_msg):
    """Internal task to scrape and batch upload."""
    from plugins.helper.upload import fetch_ytdlp_title, download_url, upload_file
    import time
    import os
    from utils.shared import get_http_session
    import aiohttp
    
    queue = SCRAPE_QUEUE.get(user_id)
    if not queue:
        return
    
    queue["active"] = True
    
    try:
        await status_msg.edit_text(
            f"🔍 **Scraping site…**\n\n"
            f"📎 URL: `{url}`\n"
            f"🎬 Max videos: {max_videos}\n\n"
            "⏳ Extracting video links…",
        )
        
        links = await scrape_category_links(url, max_videos)
        
        if not links:
            await status_msg.edit_text("❌ No video links found on this page.")
            SCRAPE_QUEUE.pop(user_id, None)
            return
        
        queue["total"] = len(links)
        
        await status_msg.edit_text(
            f"✅ **Found {len(links)} videos!**\n\n"
            f"📊 Queue: 0/{len(links)} | ❌ Failed: 0\n\n"
            "▶️ Starting uploads…"
        )
        
        for i, link_data in enumerate(links):
            if queue.get("cancel"):
                await status_msg.edit_text(
                    f"⏹️ **Scraping stopped by user!**\n\n"
                    f"📊 Uploaded: {queue['done']}/{queue['total']}\n"
                    f"❌ Failed: {queue['failed']}"
                )
                break
            
            video_url = link_data.get("url", "")
            title = link_data.get("title", f"video_{i+1}")
            thumbnail = link_data.get("thumbnail")
            
            queue["done"] = i + 1
            
            await status_msg.edit_text(
                f"📤 **Uploading {i+1}/{len(links)}**\n\n"
                f"📎 `{video_url[:60]}…`\n"
                f"📊 Progress: {queue['done']}/{queue['total']} | ❌ Failed: {queue['failed']}"
            )
            
            try:
                filename = f"{title[:80]}.mp4"
                start_time = [time.time()]
                
                file_path, mime = await download_url(
                    video_url, filename, status_msg, start_time, user_id, format_id="best"
                )
                
                if not file_path or not os.path.exists(file_path):
                    queue["failed"] += 1
                    continue
                
                user_data = {}
                thumb_file_id = user_data.get("thumb")
                
                caption = title
                
                sent = await upload_file(
                    client, chat_id, file_path, mime, caption, thumb_file_id, status_msg, start_time, user_id=user_id
                )
                
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass
                
                queue["done"] = i + 1
                
            except Exception as e:
                Config.LOGGER.error(f"Upload error for {video_url}: {e}")
                queue["failed"] += 1
                continue
            
            await asyncio.sleep(1)
        
        if not queue.get("cancel"):
            await status_msg.edit_text(
                f"✅ **Batch Upload Complete!**\n\n"
                f"📊 Total: {queue['total']}\n"
                f"✅ Uploaded: {queue['done'] - queue['failed']}\n"
                f"❌ Failed: {queue['failed']}"
            )
        
    except Exception as e:
        Config.LOGGER.error(f"Scrape error: {e}")
        import traceback
        Config.LOGGER.error(traceback.format_exc())
        await status_msg.edit_text(f"❌ Error: {e}")
    finally:
        SCRAPE_QUEUE.pop(user_id, None)
        SCRAPE_TASKS.pop(user_id, None)


@Client.on_message(filters.command("scrape_stop") & filters.private)
@admin_only
async def scrape_stop_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in SCRAPE_QUEUE:
        return await message.reply_text("❌ No active scraping job.", quote=True)
    
    SCRAPE_QUEUE[user_id]["cancel"] = True
    
    task = SCRAPE_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
    
    await message.reply_text("⏹️ Stopping scrape after current upload…", quote=True)


@Client.on_message(filters.command("scrape_status") & filters.private)
@admin_only
async def scrape_status_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in SCRAPE_QUEUE:
        return await message.reply_text("❌ No active scraping job.", quote=True)
    
    q = SCRAPE_QUEUE[user_id]
    status = "Active" if q.get("active") else "Scraping links…"
    
    await message.reply_text(
        f"📊 **Scraping Status**\n\n"
        f"📊 Total: {q['total']}\n"
        f"📤 Done: {q['done']}\n"
        f"❌ Failed: {q['failed']}\n"
        f"⏸️ Status: {status}",
        quote=True
    )
