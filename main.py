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
    match = re.match(
        r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url
    )
    if match:
        return match.group(1) + "/"
    return url


def extract_stories(url: str) -> dict:
    """Extract Instagram stories using yt-dlp with all available stories"""
    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "extract_flat": False,
        "noplaylist": False,
        "playlist_items": "1-200",
        "cookiefile": COOKIE_FILE,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return parse_story_response(info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal memproses URL: {str(e)}")


def parse_story_response(info: dict) -> dict:
    entries = info.get("entries")
    playlist_count = info.get("playlist_count", 0)
    print(f"[DEBUG] yt-dlp: playlist_count={playlist_count}, entries={len(entries) if entries else 0}")

    if not entries or len(entries) == 0:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan.")

    items = []
    for i, entry in enumerate(entries):
        url = entry.get("url", "")
        thumb = entry.get("thumbnail", "")

        # Detect video vs photo
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

    username = info.get("uploader") or info.get("channel") or ""

    return {
        "type": "carousel",
        "items": items,
        "description": info.get("title") or f"Story by {username}",
        "author": username,
    }


# ===== Routes =====

@app.get("/instagram/story")
async def get_story(url: str = Query(..., description="Instagram story URL")):
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
        "extract_flat": False,
        "noplaylist": False,
        "cookiefile": COOKIE_FILE,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries", [])
        items = []
        for entry in entries:
            is_video = entry.get("vcodec") and entry["vcodec"] != "none"
            items.append({
                "type": "video" if is_video else "photo",
                "url": entry.get("url", ""),
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
