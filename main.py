import os
import base64
import re
import json
import yt_dlp
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="ReeTools Instagram Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIE_FILE = os.environ.get("COOKIE_FILE", "instagram_cookies.txt")

# Simple in-memory cache for username -> user_id
import time as _time
_user_id_cache: dict[str, tuple[str, float]] = {}  # username -> (user_id, timestamp)

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
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}

IG_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "X-IG-App-ID": "9366197433924594",
    "X-ASBD-ID": "198387",
    "X-IG-WWW-Claim": "0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def parse_netscape_cookies() -> dict:
    """Parse Netscape cookie file into a dict of {name: value}"""
    cookies = {}
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    except Exception as e:
        print(f"Cookie parse error: {e}")
    return cookies


def get_cookie_header() -> str:
    c = parse_netscape_cookies()
    return f"sessionid={c.get('sessionid', '')}; csrftoken={c.get('csrftoken', '')}; ds_user_id={c.get('ds_user_id', '')}"


def extract_username(url: str) -> str:
    match = re.search(r"instagram\.com/stories/([\w.]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"instagram\.com/stories/highlights/(\d+)", url)
    if match:
        return ""  # highlight uses numeric ID
    return ""


async def get_user_id(username: str, client: httpx.AsyncClient, cookie_header: str) -> Optional[str]:
    """Get Instagram numeric user ID from username (with cache + retry)"""
    # Check cache (5 min TTL)
    if username in _user_id_cache:
        uid, ts = _user_id_cache[username]
        if _time.time() - ts < 300:
            print(f"[DEBUG] cache hit: {username} -> {uid}")
            return uid

    headers = {**IG_API_HEADERS, "Cookie": cookie_header}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = await client.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                headers=headers,
            )
            status = resp.status_code
            print(f"[DEBUG] get_user_id status={status} attempt={attempt+1}")

            if status == 429:
                wait = (attempt + 1) * 5
                print(f"[DEBUG] rate limited, waiting {wait}s...")
                await _asleep(wait)
                continue

            if status != 200:
                print(f"[DEBUG] get_user_id body: {resp.text[:300]}")
                return None

            data = resp.json()
            user = data.get("data", {}).get("user", {})
            uid = str(user.get("pk") or user.get("id", ""))
            if uid:
                _user_id_cache[username] = (uid, _time.time())
                print(f"[DEBUG] got user_id={uid} (cached)")
            return uid or None

        except Exception as e:
            print(f"get_user_id error: {e}")
            if attempt < max_retries - 1:
                await _asleep(2)
            else:
                return None

    return None


async def _asleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


async def extract_stories_direct(url: str) -> dict:
    """Call Instagram's reels_media API directly to get ALL stories"""
    username = extract_username(url)
    if not username:
        raise HTTPException(status_code=400, detail="Username tidak ditemukan di URL")

    cookie_header = get_cookie_header()
    headers = {**IG_API_HEADERS, "Cookie": cookie_header}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Get user ID
        user_id = await get_user_id(username, client, cookie_header)
        if not user_id:
            raise HTTPException(status_code=400, detail="Gagal mendapatkan user ID. Pastikan cookies valid.")

        print(f"[DEBUG] username={username}, user_id={user_id}")

        # Step 2: Get all stories via reels_media API (with retry)
        for attempt in range(3):
            resp = await client.get(
                f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}",
                headers=headers,
            )
            if resp.status_code == 429:
                wait = (attempt + 1) * 5
                print(f"[DEBUG] reels_media rate limited, waiting {wait}s...")
                await _asleep(wait)
                continue
            break

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Instagram API error ({resp.status_code}): {resp.text[:200]}",
            )

        data = resp.json()
        reels = data.get("reels", {})
        reel = reels.get(user_id, {})
        items_raw = reel.get("items", [])

        print(f"[DEBUG] direct API: got {len(items_raw)} story items")

        items = []
        for item in items_raw:
            media_type = item.get("media_type", 1)  # 1=photo, 2=video
            is_video = media_type == 2

            if is_video:
                video_versions = item.get("video_versions", [])
                url = video_versions[0]["url"] if video_versions else ""
                thumbnail = item.get("image_versions2", {}).get("candidates", [{}])[0].get("url", "")
            else:
                image_versions = item.get("image_versions2", {}).get("candidates", [])
                url = image_versions[0]["url"] if image_versions else ""
                thumbnail = url

            items.append({
                "type": "video" if is_video else "photo",
                "url": url,
                "thumbnail": thumbnail,
            })

        if not items:
            raise HTTPException(status_code=404, detail="Tidak ada story ditemukan. Mungkin sudah expired.")

        return {
            "type": "carousel",
            "items": items,
            "description": f"Story by {username}",
            "author": username,
        }


# ===== yt-dlp fallback (for non-story content) =====

def clean_story_url(url: str) -> str:
    match = re.match(
        r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url
    )
    if match:
        return match.group(1) + "/"
    return url


def extract_info(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": False,
        "cookiefile": COOKIE_FILE,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Gagal memproses URL: {str(e)}"
        )


def is_video_media(entry: dict) -> bool:
    if entry.get("vcodec") and entry["vcodec"] != "none":
        return True
    if entry.get("acodec") and entry["acodec"] != "none":
        return True
    ext = entry.get("ext", "")
    if ext in ("mp4", "webm", "mov"):
        return True
    if entry.get("duration"):
        return True
    url = entry.get("url", "")
    if ".mp4" in url or "video" in url.lower():
        return True
    return False


def get_media_url(entry: dict) -> str:
    url = entry.get("url", "")
    if url and ("cdninstagram" in url or "fbcdn" in url):
        return url
    if url and url.startswith("http"):
        return url
    return entry.get("webpage_url", "")


def parse_response(info: dict) -> dict:
    entries = info.get("entries")
    if entries:
        items = []
        for entry in entries:
            video = is_video_media(entry)
            items.append({
                "type": "video" if video else "photo",
                "url": get_media_url(entry),
                "thumbnail": entry.get("thumbnail", ""),
            })
        result = {
            "type": "carousel",
            "items": items,
            "description": info.get("title") or info.get("description", ""),
            "author": info.get("uploader") or info.get("channel", ""),
        }
        if info.get("title"):
            result["title"] = info.get("title")
        return result

    video = is_video_media(info)
    result = {
        "type": "video" if video else "photo",
        "url": get_media_url(info),
        "thumbnail": info.get("thumbnail", ""),
        "description": info.get("description") or info.get("title", ""),
        "author": info.get("uploader") or info.get("channel", ""),
    }
    if info.get("title"):
        result["title"] = info.get("title")
    return result


# ===== Routes =====

@app.get("/instagram/story")
async def get_story(url: str = Query(..., description="Instagram story URL")):
    try:
        # Try direct API first (more reliable, gets ALL stories)
        return JSONResponse(await extract_stories_direct(url))
    except HTTPException:
        raise
    except Exception as e:
        print(f"Direct API failed, falling back to yt-dlp: {e}")
        # Fallback to yt-dlp
        try:
            clean_url = clean_story_url(url)
            info = extract_info(clean_url)
            return JSONResponse(parse_response(info))
        except HTTPException:
            raise
        except Exception as e2:
            raise HTTPException(status_code=500, detail=str(e2))


@app.get("/instagram/highlight")
async def get_highlight(url: str = Query(..., description="Instagram highlight URL")):
    try:
        clean_url = clean_story_url(url)
        info = extract_info(clean_url)
        return JSONResponse(parse_response(info))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
