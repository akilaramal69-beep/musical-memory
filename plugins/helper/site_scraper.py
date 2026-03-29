import asyncio
import re
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse, unquote
from bs4 import BeautifulSoup
from plugins.config import Config
from utils.shared import get_http_session


async def scrape_category_links(url: str, max_videos: int = 50) -> List[Dict]:
    """
    Scrape ONLY video links from the SPECIFIC category page provided.
    Does NOT follow pagination or navigate to other sections.
    """
    scraped = []
    seen_urls = set()
    
    try:
        session = await get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": url,
        }
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30), proxy=Config.PROXY, ssl=False) as resp:
            if resp.status != 200:
                Config.LOGGER.error(f"Scrape failed: HTTP {resp.status}")
                return []
            
            html = await resp.text()
        
        base_domain = urlparse(url).netloc
        base_path = urlparse(url).path.rstrip('/')
        
        soup = BeautifulSoup(html, 'html.parser')
        
        video_links = extract_video_links_from_soup(soup, url, base_domain, base_path)
        
        for link_data in video_links:
            if len(scraped) >= max_videos:
                break
            
            video_url = link_data.get("url", "")
            if video_url in seen_urls:
                continue
            seen_urls.add(video_url)
            scraped.append(link_data)
            
            Config.LOGGER.info(f"Scraped: {video_url[:80]}")
        
        Config.LOGGER.info(f"Total scraped from {url}: {len(scraped)} videos")
        return scraped
        
    except asyncio.TimeoutError:
        Config.LOGGER.error(f"Timeout scraping {url}")
        return []
    except Exception as e:
        Config.LOGGER.error(f"Error scraping {url}: {e}")
        return []


def extract_video_links_from_soup(soup: BeautifulSoup, page_url: str, base_domain: str, base_path: str) -> List[Dict]:
    """
    Extract video links ONLY from the current page.
    Strictly filters out pagination and non-video links.
    """
    results = []
    
    video_selectors = [
        'a[href*="/video/"]',
        'a[href*="/view/"]',
        'a[href*="/watch/"]',
        'a[href*="/videos/"]',
        'div.video-item a',
        'div.thumb a',
        'li.video a',
        'div.portal-video-item a',
        '.video-box a',
        '.video-thumb a',
        '.video-list a',
        'a[data-video-id]',
        'a.js-mediabox',
        'a[href*="?id="]',
    ]
    
    for selector in video_selectors:
        for a_tag in soup.select(selector):
            try:
                href = a_tag.get('href', '')
                
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue
                
                if href.startswith('//'):
                    href = 'https:' + href
                elif href.startswith('/'):
                    href = urljoin(page_url, href)
                
                if not href.startswith('http'):
                    continue
                
                link_domain = urlparse(href).netloc
                if base_domain not in link_domain and 'cdn' not in link_domain:
                    continue
                
                if any(x in href.lower() for x in ['/categories/', '/pornstars/', '/channels/', '/search', '/signup', '/login', '/premium', '/album']):
                    continue
                
                if any(x in href.lower() for x in ['page=', 'paging', 'pagination']):
                    continue
                
                title = extract_video_title(a_tag)
                
                thumbnail = None
                img = a_tag.find('img') or a_tag.find('video')
                if img:
                    thumbnail = img.get('src') or img.get('data-src') or img.get('data-thumbnail')
                    if thumbnail and thumbnail.startswith('//'):
                        thumbnail = 'https:' + thumbnail
                
                results.append({
                    "url": href,
                    "title": title,
                    "thumbnail": thumbnail
                })
                
            except Exception:
                continue
    
    seen_urls = set()
    unique_results = []
    for item in results:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            unique_results.append(item)
    
    return unique_results


def extract_video_title(a_tag) -> str:
    """Extract the best title from an anchor tag."""
    title = None
    
    if a_tag.get('title') and len(a_tag.get('title', '')) > 3:
        title = a_tag.get('title')
    elif a_tag.get('data-title'):
        title = a_tag.get('data-title')
    
    if not title:
        span = a_tag.find('span', class_=lambda x: x and 'title' in x.lower()) if a_tag else None
        if span:
            title = span.get_text(strip=True)
    
    if not title:
        img = a_tag.find('img') if a_tag else None
        if img:
            title = img.get('alt') or img.get('title')
    
    if not title:
        text = a_tag.get_text(strip=True) if a_tag else ""
        if text and len(text) > 3:
            title = text[:100]
    
    if not title:
        href = a_tag.get('href', '') if a_tag else ''
        title = extract_title_from_url(href)
    
    title = re.sub(r'\s+', ' ', title or '').strip()
    title = re.sub(r'[^\w\s\-\(\)\.\!\?]', '', title)
    
    return title[:100] if title else "Untitled Video"


def extract_title_from_url(url: str) -> str:
    """Fallback: Extract title from URL path."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    
    parts = [p for p in path.split('/') if p and not any(x in p.lower() for x in ['video', 'watch', 'view', 'html', 'php', 'page'])]
    
    if parts:
        title = parts[-1]
        title = unquote(title)
        title = re.sub(r'[-_]', ' ', title)
        title = re.sub(r'\.(?:mp4|m3u8|webm|avi|mov|mkv)$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'[0-9a-f]{8,}', '', title, flags=re.IGNORECASE)
        title = title.strip()
        if title:
            return title.title()
    
    return "Untitled Video"


async def get_video_metadata(url: str) -> Optional[Dict]:
    """Get video metadata from a single video page."""
    try:
        session = await get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), proxy=Config.PROXY, ssl=False) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
        
        soup = BeautifulSoup(html, 'html.parser')
        
        title = None
        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '').strip()
        
        if not title:
            title_tag = soup.find('title')
            if title_tag:
                title = title_tag.get_text(strip=True)
                title = re.sub(r'\s*[-|].*$', '', title).strip()
        
        thumbnail = None
        og_image = soup.find('meta', property='og:image')
        if og_image:
            thumbnail = og_image.get('content', '').strip()
        
        video_url = None
        for pattern in ['"contentUrl"', '"url"', 'data-video-url', 'data-src']:
            elem = soup.find(attrs={'data-video-url': True}) or soup.find('source')
            if elem:
                video_url = elem.get('src') or elem.get('data-video-url')
                if video_url:
                    if video_url.startswith('//'):
                        video_url = 'https:' + video_url
                    break
        
        return {
            "url": video_url or url,
            "title": title or extract_title_from_url(url),
            "thumbnail": thumbnail,
            "page_url": url
        }
        
    except Exception as e:
        Config.LOGGER.error(f"Error getting metadata for {url}: {e}")
        return None
