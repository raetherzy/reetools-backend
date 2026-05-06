import os
import base64
import re
import json
import yt_dlp
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI(title="ReeTools Instagram Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIE_FILE = os.environ.get("COOKIE_FILE", "instagram_cookies.txt")

@app.on_event("startup")
async def startup():
    cookies_b64 = os.environ.get("INSTAGRAM_COOKIES_B64", "")
    if cookies_b64:
        try:
            cookies = base64.b64decode(cookies_b64).decode("utf-8")
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                f.write(cookies.strip() + "\n")
        except Exception as e:
            print(f"Failed to decode cookies: {e}")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
}


def parse_netscape_cookies(filepath: str) -> dict:
    """Parse Netscape cookie file into dict of {name: value}"""
    cookies = {}
    if not filepath:
        print("[WARN] Cookie filepath is None/empty")
        return cookies
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[WARN] Cookie file not found: {filepath}")
        return cookies

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            cookies[name] = value
            continue

    if not cookies:
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for pair in line.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    cookies[key.strip()] = val.strip().strip('"')

    return cookies


def build_cookie_string(filepath: str = None) -> str:
    if filepath is None:
        filepath = COOKIE_FILE
    c = parse_netscape_cookies(filepath)
    return f"sessionid={c.get('sessionid','')}; csrftoken={c.get('csrftoken','')}; ds_user_id={c.get('ds_user_id','')}"


def make_web_headers(filepath: str = None) -> dict:
    if filepath is None:
        filepath = COOKIE_FILE
    h = dict(BROWSER_HEADERS)
    h["Cookie"] = build_cookie_string(filepath)
    h["X-CSRFToken"] = parse_netscape_cookies(filepath).get("csrftoken", "")
    return h


async def extract_stories_from_page(username: str) -> dict:
    """
    Fetch Instagram story viewer page HTML dan extract SEMUA story dari
    embedded JSON data — 1 request ke www.instagram.com, tidak kena rate limit API.
    """
    headers = make_web_headers()
    page_url = f"https://www.instagram.com/stories/{username}/"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(page_url, headers=headers)
        print(f"[DEBUG] Page status: {resp.status_code}")
        if resp.status_code != 200:
            raise Exception(f"Gagal fetch halaman: HTTP {resp.status_code}")
        html = resp.text

    all_items = []
    script_pattern = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL)
    for match in script_pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = _dig_json_for_items(data)
        if items:
            all_items.extend(items)

    if not all_items:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan di halaman.")

    print(f"[INFO] Page scrape: {len(all_items)} total item dari HTML")
    for i, item in enumerate(all_items):
        print(f"[DEBUG] scrape item[{i}]: {'video' if item['type']=='video' else 'photo'}, url={item['url'][:80]}")

    return {
        "type": "carousel",
        "items": all_items,
        "description": f"Story by {username}",
        "author": username,
    }


def _dig_json_for_items(obj, depth=0):
    """Rekursif cari story items di dalam nested JSON"""
    if depth > 15:
        return []
    if isinstance(obj, dict):
        is_reel_item = ("image_versions2" in obj or "video_versions" in obj) and obj.get("pk")
        if is_reel_item:
            parsed = _parse_item_from_json(obj)
            if parsed:
                if isinstance(parsed, list):
                    return parsed
                return [parsed]
        if "items" in obj and isinstance(obj["items"], list):
            results = []
            for sub in obj["items"]:
                if isinstance(sub, dict):
                    results.extend(_dig_json_for_items(sub, depth + 1))
            if results:
                return results
        for v in obj.values():
            found = _dig_json_for_items(v, depth + 1)
            if found:
                return found
        return []
    if isinstance(obj, list):
        for item in obj:
            found = _dig_json_for_items(item, depth + 1)
            if found:
                return found
    return []


def _parse_item_from_json(item: dict):
    videos = item.get("video_versions", [])
    images = item.get("image_versions2", {}).get("candidates", [])
    carousel = item.get("carousel_media", [])

    if carousel:
        results = []
        for sub in carousel:
            parsed = _parse_single_media(sub)
            if parsed:
                results.append(parsed)
        return results

    return _parse_single_media(item)


def _parse_single_media(item: dict):
    videos = item.get("video_versions", [])
    images = item.get("image_versions2", {}).get("candidates", [])
    is_video = bool(videos)

    if is_video and videos:
        url = videos[0].get("url", "")
    elif images:
        url = images[0].get("url", "")
    else:
        return None

    if not url or not url.startswith("http"):
        return None

    thumb = images[0].get("url", "") if images else ""
    return {"type": "video" if is_video else "photo", "url": url, "thumbnail": thumb}


def clean_story_url(url: str) -> str:
    match = re.match(
        r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url
    )
    if match:
        return match.group(1) + "/"
    return url


def extract_stories(url: str) -> dict:
    """Extract ALL stories — yt-dlp flat playlist lalu resolve satu per satu"""
    username = extract_username_from_url(url)
    print(f"[INFO] Extract stories untuk @{username}...")

    ydl_opts_flat = {
        "quiet": False,
        "no_warnings": False,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "cookiefile": COOKIE_FILE,
        "ignoreerrors": True,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts_flat) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal extract playlist: {str(e)}")

    flat_entries = info.get("entries") or []
    print(f"[INFO] Flat playlist: {len(flat_entries)} entries")

    if not flat_entries:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan.")

    items = []
    ydl_opts_resolve = {
        "quiet": False,
        "no_warnings": False,
        "extract_flat": False,
        "noplaylist": True,
        "cookiefile": COOKIE_FILE,
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "sleep_interval": 1.5,
        "max_sleep_interval": 4,
    }

    import time
    for i, entry in enumerate(flat_entries):
        entry_url = entry.get("url") or entry.get("webpage_url") or ""
        if not entry_url:
            print(f"[WARN] entry[{i}] tidak punya URL, skip")
            continue

        print(f"[INFO] Resolving {i+1}/{len(flat_entries)}: {entry_url[:80]}...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts_resolve) as ydl:
                resolved = ydl.extract_info(entry_url, download=False)

            media_url = resolved.get("url", "")
            if not media_url or not media_url.startswith("http"):
                media_url = resolved.get("requested_downloads", [{}])[0].get("url", "")

            if not media_url or not media_url.startswith("http"):
                print(f"[WARN] entry[{i}] resolved tapi URL kosong, skip")
                continue

            is_video = False
            if resolved.get("vcodec") and resolved["vcodec"] != "none":
                is_video = True
            elif resolved.get("duration"):
                is_video = True
            elif ".mp4" in media_url.lower():
                is_video = True

            items.append({
                "type": "video" if is_video else "photo",
                "url": media_url,
                "thumbnail": resolved.get("thumbnail", ""),
            })
            print(f"[DEBUG] resolved entry[{i}]: {'video' if is_video else 'photo'}, url={media_url[:80]}")

        except Exception as e:
            print(f"[WARN] entry[{i}] gagal resolve: {e}")

        time.sleep(1)

    if not items:
        raise HTTPException(status_code=404, detail="Semua entry gagal di-resolve.")

    return {
        "type": "carousel",
        "items": items,
        "description": info.get("title") or f"Story by {username}",
        "author": info.get("uploader") or info.get("channel") or username,
    }


def extract_username_from_url(url: str) -> str:
    """Extract username from Instagram story URL"""
    match = re.search(r"instagram\.com/stories/([\w.]+)", url)
    if match:
        return match.group(1)
    raise HTTPException(status_code=400, detail="Tidak bisa extract username dari URL")


def parse_story_response(info: dict) -> dict:
    entries = info.get("entries")
    playlist_count = info.get("playlist_count", 0)
    print(f"[DEBUG] yt-dlp: playlist_count={playlist_count}, entries={len(entries) if entries else 0}")

    if not entries or len(entries) == 0:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan.")

    items = []
    for i, entry in enumerate(entries):
        url = entry.get("url", "")

        if not url or not url.startswith("http"):
            print(f"[WARN] entry[{i}] dilewati karena URL tidak valid: {str(url)[:50]}")
            continue

        thumb = entry.get("thumbnail", "")

        is_video = False
        if entry.get("vcodec") and entry["vcodec"] != "none":
            is_video = True
        elif entry.get("duration"):
            is_video = True
        elif ".mp4" in url.lower():
            is_video = True

        items.append({
            "type": "video" if is_video else "photo",
            "url": url,
            "thumbnail": thumb,
        })
        print(f"[DEBUG]   item[{i}]: {'video' if is_video else 'photo'}, url={url[:80]}...")

    if not items:
        raise HTTPException(status_code=404, detail="Semua entry tidak valid.")

    username = info.get("uploader") or info.get("channel") or ""

    return {
        "type": "carousel",
        "items": items,
        "description": info.get("title") or f"Story by {username}",
        "author": username,
    }


# ===== Routes =====

@app.get("/instagram/story")
def get_story(url: str = Query(..., description="Instagram story URL")):
    clean_url = clean_story_url(url)
    print(f"[DEBUG] story request: {url} -> {clean_url}")
    try:
        return JSONResponse(extract_stories(clean_url))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instagram/highlight")
async def get_highlight(url: str = Query(..., description="Instagram highlight URL")):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "cookiefile": COOKIE_FILE,
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info.get("entries"):
                resolved_entries = []
                for entry in info["entries"]:
                    if entry.get("url") and entry["url"].startswith("http"):
                        resolved_entries.append(entry)
                    else:
                        entry_url = entry.get("webpage_url") or entry.get("url", "")
                        if entry_url:
                            try:
                                resolved = ydl.extract_info(entry_url, download=False)
                                resolved_entries.append(resolved)
                            except Exception as e:
                                print(f"[WARN] Highlight, gagal resolve entry: {e}")
                                continue
                info["entries"] = resolved_entries

        entries = info.get("entries", [])
        items = []
        for entry in entries:
            url = entry.get("url", "")
            if not url or not url.startswith("http"):
                continue
            is_video = entry.get("vcodec") and entry["vcodec"] != "none"
            items.append({
                "type": "video" if is_video else "photo",
                "url": url,
                "thumbnail": entry.get("thumbnail", ""),
            })

        return JSONResponse({
            "type": "carousel",
            "items": items,
            "description": info.get("title", ""),
            "author": info.get("uploader", ""),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instagram/story/debug")
async def debug_story(username: str = Query(..., description="Instagram username")):
    """Endpoint debug — gunakan yt-dlp flat + resolve satu per satu"""
    try:
        url = f"https://www.instagram.com/stories/{username}/"
        result = extract_stories(url)
        return {
            "method": "ytdlp_flat_then_resolve",
            "total_items": len(result["items"]),
            "items": [
                {"index": i, "type": it["type"], "url_preview": it["url"][:80]}
                for i, it in enumerate(result["items"])
            ],
        }
    except HTTPException as e:
        return JSONResponse({"error": e.detail, "method": "ytdlp_flat_then_resolve"}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e), "method": "ytdlp_flat_then_resolve"}, status_code=500)


@app.get("/instagram/stream")
async def stream_media(
    url: str = Query(..., description="Media CDN URL"),
    download: Optional[str] = Query(None),
    filename: Optional[str] = Query("instagram_media"),
    type: Optional[str] = Query("video"),
):
    async def streamer():
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "GET", url, headers=BROWSER_HEADERS, follow_redirects=True,
            ) as r:
                async for chunk in r.aiter_bytes(8192):
                    yield chunk

    content_type = "video/mp4" if type == "video" else "image/jpeg"
    resp_headers = {}
    if download:
        ext = "mp4" if type == "video" else "jpg"
        resp_headers["Content-Disposition"] = f'attachment; filename="{filename}.{ext}"'

    return StreamingResponse(streamer(), media_type=content_type, headers=resp_headers)


@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
