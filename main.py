import os
import base64
import re
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


def clean_story_url(url: str) -> str:
    # Convert specific story URL to base stories URL to get ALL stories
    # From: /stories/username/123456789/
    # To:   /stories/username/
    match = re.match(
        r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url
    )
    if match:
        return match.group(1) + "/"
    return url

def extract_info(url: str) -> dict:
    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "extract_flat": False,
        "noplaylist": False,
        "playlist_items": "1-100",
        "playlistend": 0,
        "cookiefile": COOKIE_FILE,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Gagal memproses URL: {str(e)}"
        )


def is_video_media(entry: dict) -> bool:
    # Multiple ways to detect video
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
    fmt = entry.get("format", "")
    if fmt and "video" in fmt:
        return True
    return False

def get_media_url(entry: dict) -> str:
    # Prefer direct CDN URL, fallback to webpage_url
    url = entry.get("url", "")
    if url and ("cdninstagram" in url or "fbcdn" in url or "video" in url.lower() or ".mp4" in url or ".jpg" in url):
        return url
    if url and url.startswith("http"):
        return url
    return entry.get("webpage_url", "")

def parse_response(info: dict) -> dict:
    entries = info.get("entries")
    print(f"[DEBUG] entries count: {len(entries) if entries else 0}")
    print(f"[DEBUG] info keys: {list(info.keys())}")

    if entries:
        items = []
        for i, entry in enumerate(entries):
            video = is_video_media(entry)
            url = get_media_url(entry)
            print(f"[DEBUG] entry[{i}]: type={'video' if video else 'photo'}, url={url[:80]}...")
            items.append({
                "type": "video" if video else "photo",
                "url": url,
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

    # Single media
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


@app.get("/instagram/story")
async def get_story(url: str = Query(..., description="Instagram story URL")):
    try:
        clean_url = clean_story_url(url)
        info = extract_info(clean_url)
        return JSONResponse(parse_response(info))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
                "GET",
                url,
                headers=BROWSER_HEADERS,
                follow_redirects=True,
            ) as r:
                async for chunk in r.aiter_bytes(8192):
                    yield chunk

    content_type = "video/mp4" if type == "video" else "image/jpeg"
    headers = {}
    if download:
        ext = "mp4" if type == "video" else "jpg"
        headers["Content-Disposition"] = f'attachment; filename="{filename}.{ext}"'

    return StreamingResponse(
        streamer(),
        media_type=content_type,
        headers=headers,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
