import asyncio
import re
import aiohttp
from typing import List, Dict
from urllib.parse import urljoin, urlparse, unquote
from plugins.config import Config
from utils.shared import get_http_session


async def scrape_category_links(url: str, max_videos: int = 50) -> List[Dict]:
    """
    Scrape video links from a category page.
    """
    try:
        Config.LOGGER.info(f"Starting scrape for: {url}")
        
        session = await get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        timeout = aiohttp.ClientTimeout(total=30)
        async with session.get(url, headers=headers, timeout=timeout, ssl=False) as resp:
            Config.LOGGER.info(f"Response status: {resp.status}")
            if resp.status != 200:
                Config.LOGGER.error(f"HTTP error: {resp.status}")
                return []
            html = await resp.text()
        
        Config.LOGGER.info(f"Got HTML length: {len(html)} chars")
        
        return extract_video_links(html, url)
        
    except asyncio.TimeoutError:
        Config.LOGGER.error("Timeout during scraping")
        return []
    except Exception as e:
        Config.LOGGER.error(f"Scraping error: {e}")
        return []


def extract_video_links(html: str, page_url: str) -> List[Dict]:
    """Extract video links from HTML using regex patterns."""
    results = []
    seen = set()
    
    base_domain = urlparse(page_url).netloc
    
    href_patterns = [
        r'href="(/video/[^"]+)"',
        r'href="(/view/[^"]+)"',
        r'href="(/watch/[^"]+)"',
        r'href="(/videos/[^"]+)"',
        r'href="(https?://[^"]*' + re.escape(base_domain) + r'[^"]*video[^"]*)"',
        r'href="([^"]+video[^"]*\.(?:mp4|m3u8|webm)[^"]*)"',
        r"href='(/video/[^']+)'",
        r"href='(https?://[^']+)'",
    ]
    
    for pattern in href_patterns:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            
            if not match:
                continue
            
            if match.startswith('//'):
                url = 'https:' + match
            elif match.startswith('/'):
                url = urljoin(page_url, match)
            else:
                url = match
            
            if not url.startswith('http'):
                continue
            
            if url in seen:
                continue
            if any(x in url.lower() for x in ['page=', 'paging', '/categories/', '/pornstars/', '/channels/', '/signup', '/login']):
                continue
            if any(x in url.lower() for x in ['.jpg', '.png', '.gif', '.jpeg', '.webp', '.css', '.js', '.png']):
                continue
            
            seen.add(url)
            
            title = extract_title(url)
            results.append({
                "url": url,
                "title": title,
                "thumbnail": None
            })
    
    Config.LOGGER.info(f"Found {len(results)} video links")
    return results[:100]


def extract_title(url: str) -> str:
    """Extract a readable title from URL."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    
    parts = path.split('/')
    for part in reversed(parts):
        if part and not any(x in part.lower() for x in ['video', 'watch', 'view', 'html', 'php', 'page']):
            title = unquote(part)
            title = re.sub(r'[-_]', ' ', title)
            title = re.sub(r'\.(?:mp4|m3u8|webm|avi|mov|mkv)$', '', title, flags=re.IGNORECASE)
            title = re.sub(r'[0-9a-f]{8,}', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\d+$', '', title)
            title = title.strip()
            if title and len(title) > 2:
                return title.title()
    
    return f"Video {hash(url) % 10000}"
