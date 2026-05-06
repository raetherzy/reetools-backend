import os
import re
import time
import base64
import httpx
import yt_dlp
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
            print("[OK] Cookies loaded from env")
        except Exception as e:
            print(f"[ERROR] Failed to decode cookies: {e}")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}

def load_cookies() -> dict:
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
    except FileNotFoundError:
        pass
    return cookies

def build_api_headers() -> dict:
    c = load_cookies()
    return {
        "User-Agent": "Instagram 219.0.0.12.117 Android",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
        "X-CSRFToken": c.get("csrftoken", ""),
        "Cookie": f"sessionid={c.get('sessionid','')}; csrftoken={c.get('csrftoken','')}; ds_user_id={c.get('ds_user_id','')}",
    }

async def ig_get_user_id(username: str) -> str:
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=build_api_headers())
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Gagal ambil user info: HTTP {resp.status_code}")
    data = resp.json()
    uid = data.get("data", {}).get("user", {}).get("id")
    if not uid:
        raise HTTPException(status_code=404, detail=f"User @{username} tidak ditemukan")
    return str(uid)

def parse_media_item(item: dict) -> Optional[dict]:
    videos = item.get("video_versions", [])
    images = item.get("image_versions2", {}).get("candidates", [])
    is_video = bool(videos)
    if is_video and videos:
        url = videos[0].get("url", "")
    elif images:
        url = images[0].get("url", "")
    else:
        return None
    if not url.startswith("http"):
        return None
    return {
        "type": "video" if is_video else "photo",
        "url": url,
        "thumbnail": images[0].get("url", "") if images else "",
    }

async def extract_stories_direct(username: str) -> list:
    user_id = await ig_get_user_id(username)
    url = f"https://i.instagram.com/api/v1/feed/user/{user_id}/story/"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=build_api_headers())
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Gagal fetch story: HTTP {resp.status_code}")
    data = resp.json()
    reel = data.get("reel") or data.get("reels", {}).get(user_id) or {}
    raw = reel.get("items", [])
    print(f"[INFO] Direct API: {len(raw)} raw items")
    items = []
    for item in raw:
        mt = item.get("media_type", 1)
        if mt == 8:
            for sub in item.get("carousel_media", []):
                p = parse_media_item(sub)
                if p: items.append(p)
        else:
            p = parse_media_item(item)
            if p: items.append(p)
    print(f"[INFO] Direct API parsed: {len(items)} items")
    return items

def extract_stories_ytdlp(url: str) -> list:
    flat_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist", "noplaylist": False,
        "cookiefile": COOKIE_FILE, "ignoreerrors": True,
        "playlistend": 999, "playlist_items": "1:999",
        "sleep_interval": 2, "max_sleep_interval": 5,
        "extractor_args": {
            "instagram": {
                "include_stories": True,
            }
        },
    }
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    flat_entries = info.get("entries") or []
    print(f"[INFO] yt-dlp flat playlist: {len(flat_entries)} entries")
    if not flat_entries:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan")

    resolve_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": False, "noplaylist": True,
        "cookiefile": COOKIE_FILE, "ignoreerrors": True,
        "retries": 5, "fragment_retries": 5,
        "sleep_interval": 1.5, "max_sleep_interval": 4,
    }
    items = []
    for i, entry in enumerate(flat_entries):
        entry_url = entry.get("url") or entry.get("webpage_url") or ""
        if not entry_url:
            continue
        try:
            with yt_dlp.YoutubeDL(resolve_opts) as ydl:
                resolved = ydl.extract_info(entry_url, download=False)
            media_url = resolved.get("url", "")
            if not media_url.startswith("http"):
                media_url = (resolved.get("requested_downloads") or [{}])[0].get("url", "")
            if not media_url.startswith("http"):
                continue
            is_video = (
                (resolved.get("vcodec") and resolved["vcodec"] != "none")
                or bool(resolved.get("duration"))
                or ".mp4" in media_url.lower()
            )
            items.append({
                "type": "video" if is_video else "photo",
                "url": media_url,
                "thumbnail": resolved.get("thumbnail", ""),
            })
            print(f"[OK] yt-dlp [{i+1}/{len(flat_entries)}]: {items[-1]['type']}")
        except Exception as e:
            print(f"[WARN] yt-dlp entry {i+1} gagal: {e}")
        time.sleep(1)
    print(f"[INFO] yt-dlp resolved: {len(items)} items")
    return items

def clean_story_url(url: str) -> str:
    m = re.match(r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url)
    return m.group(1) + "/" if m else url

def extract_username_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/stories/([\w.]+)", url)
    if m:
        return m.group(1)
    raise HTTPException(status_code=400, detail="Tidak bisa extract username dari URL")

@app.get("/instagram/story")
def get_story(url: str = Query(..., description="Instagram story URL")):
    clean_url = clean_story_url(url)
    username = extract_username_from_url(clean_url)
    print(f"[INFO] Story request for @{username}")

    items = []
    try:
        items = extract_stories_ytdlp(clean_url)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] yt-dlp gagal: {e}")

    if not items:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan")
    return JSONResponse({
        "type": "carousel",
        "items": items,
        "description": f"Story by {username}",
        "author": username,
    })

@app.get("/instagram/highlight")
def get_highlight(url: str = Query(..., description="Instagram highlight URL")):
    ydl_opts = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist", "noplaylist": False,
        "cookiefile": COOKIE_FILE, "ignoreerrors": True,
        "retries": 5, "fragment_retries": 5,
        "sleep_interval": 1, "max_sleep_interval": 3,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or []
        if not entries:
            raise HTTPException(status_code=404, detail="Tidak ada highlight ditemukan")
        items = []
        for entry in entries:
            item_url = entry.get("url", "")
            if not item_url.startswith("http"):
                continue
            is_video = entry.get("vcodec") and entry["vcodec"] != "none"
            items.append({
                "type": "video" if is_video else "photo",
                "url": item_url,
                "thumbnail": entry.get("thumbnail", ""),
            })
        if not items:
            raise HTTPException(status_code=404, detail="Tidak ada highlight ditemukan")
        return JSONResponse({
            "type": "carousel",
            "items": items,
            "description": info.get("title", ""),
            "author": info.get("uploader", ""),
        })
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
            async with client.stream("GET", url, headers=BROWSER_HEADERS, follow_redirects=True) as r:
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
    c = load_cookies()
    return {"status": "ok", "cookies_loaded": bool(c.get("sessionid"))}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
