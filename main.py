import os
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
    cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if cookies:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookies.strip() + "\n")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}


def extract_info(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
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


def parse_response(info: dict) -> dict:
    # Handle playlist (multiple entries = carousel/highlight)
    entries = info.get("entries")
    if entries:
        items = []
        for entry in entries:
            is_video = entry.get("vcodec") not in ("none", None) or entry.get("acodec") not in ("none", None)
            items.append({
                "type": "video" if is_video else "photo",
                "url": entry.get("url") or entry.get("webpage_url", ""),
                "thumbnail": entry.get("thumbnail", ""),
            })

        result = {
            "type": "carousel",
            "items": items,
            "description": info.get("title") or info.get("description", ""),
            "author": info.get("uploader") or info.get("channel", ""),
        }

        # For highlights, include the title
        if info.get("title"):
            result["title"] = info.get("title")
        return result

    # Single media
    is_video = info.get("vcodec") not in ("none", None) or info.get("acodec") not in ("none", None)

    result = {
        "type": "video" if is_video else "photo",
        "url": info.get("url") or info.get("webpage_url", ""),
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
        info = extract_info(url)
        return JSONResponse(parse_response(info))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instagram/highlight")
async def get_highlight(url: str = Query(..., description="Instagram highlight URL")):
    try:
        info = extract_info(url)
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
