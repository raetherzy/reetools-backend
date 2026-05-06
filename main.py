import os
import base64
import re
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
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}

IG_APP_HEADERS = {
    "User-Agent": "Instagram 219.0.0.12.117 Android",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
}

IG_WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}


def parse_netscape_cookies(filepath: str) -> dict:
    """Parse Netscape cookie file into dict of {name: value}"""
    cookies = {}
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


def get_ig_api_headers(filepath: str = None, use_web: bool = False) -> dict:
    """Build headers for Instagram internal API requests"""
    if filepath is None:
        filepath = COOKIE_FILE
    c = parse_netscape_cookies(filepath)
    csrftoken = c.get("csrftoken", "")
    sessionid = c.get("sessionid", "")
    ds_user_id = c.get("ds_user_id", "")

    cookie_str = f"sessionid={sessionid}; csrftoken={csrftoken}; ds_user_id={ds_user_id}"
    base = dict(IG_WEB_HEADERS if use_web else IG_APP_HEADERS)
    base["X-CSRFToken"] = csrftoken
    base["Cookie"] = cookie_str
    return base


async def get_instagram_user_id(username: str) -> str:
    """Get Instagram user ID from username via internal API"""
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    headers = get_ig_api_headers()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise Exception(f"Gagal ambil user info: HTTP {resp.status_code}")
        data = resp.json()
        user = data.get("data", {}).get("user")
        if not user:
            raise Exception("User tidak ditemukan")
        return str(user["id"])


def _parse_story_item(item: dict, idx: str, force_video: bool = None) -> dict | None:
    """Parse satu story item jadi dict {type, url, thumbnail}"""
    media_type = item.get("media_type", 1)
    video_versions = item.get("video_versions", [])
    image_candidates = item.get("image_versions2", {}).get("candidates", [])

    if force_video is not None:
        is_video = force_video
    else:
        is_video = media_type in (2, 3) or bool(video_versions)

    if is_video and video_versions:
        media_url = video_versions[0].get("url", "")
    elif image_candidates:
        media_url = image_candidates[0].get("url", "")
    else:
        # fallback: cek url langsung
        media_url = item.get("url", "")
        if not media_url or not media_url.startswith("http"):
            print(f"[WARN] item[{idx}] tidak punya URL sama sekali, skip")
            return None

    if not media_url or not media_url.startswith("http"):
        print(f"[WARN] item[{idx}] URL tidak valid: {str(media_url)[:60]}")
        return None

    thumb = ""
    if image_candidates:
        thumb = image_candidates[0].get("url", "")

    print(f"[DEBUG] parsed item[{idx}]: {'video' if is_video else 'photo'}, url={media_url[:80]}")

    return {
        "type": "video" if is_video else "photo",
        "url": media_url,
        "thumbnail": thumb,
    }


async def extract_stories_direct(username: str) -> dict:
    """Extract ALL stories using Instagram internal API directly"""
    user_id = await get_instagram_user_id(username)

    all_items_raw = []
    combos = [
        ("i.instagram.com", False),
        ("i.instagram.com", True),
        ("www.instagram.com", True),
    ]

    for host, use_web in combos:
        headers = get_ig_api_headers(use_web=use_web)
        ep_url = f"https://{host}/api/v1/feed/user/{user_id}/story/"

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(ep_url, headers=headers)
                print(f"[DEBUG] {host} (web_headers={use_web}) status: {resp.status_code}")

                if resp.status_code != 200:
                    continue
                data = resp.json()

                reel = data.get("reel") or data.get("reels", {}).get(user_id)
                if reel:
                    batch = reel.get("items", [])
                    print(f"[INFO] {host}: {len(batch)} items")
                    if len(batch) > len(all_items_raw):
                        all_items_raw = batch
        except Exception as e:
            print(f"[WARN] {host} gagal: {e}")
            continue

    if not all_items_raw:
        raise HTTPException(status_code=404, detail="Tidak ada story ditemukan.")

    print(f"[INFO] Total raw story items dari semua endpoint: {len(all_items_raw)}")

    for i, item in enumerate(all_items_raw):
        print(f"[DEBUG] raw item[{i}]: media_type={item.get('media_type')}, id={item.get('pk')}, taken_at={item.get('taken_at')}")

    items = []
    for i, item in enumerate(all_items_raw):
        media_type = item.get("media_type", 1)

        if media_type == 8:
            subs = item.get("carousel_media", [])
            for ei, sub in enumerate(subs):
                parsed = _parse_story_item(sub, f"{i}.{ei}")
                if parsed:
                    items.append(parsed)
        elif media_type in (2, 3):
            parsed = _parse_story_item(item, str(i), force_video=True)
            if parsed:
                items.append(parsed)
        elif media_type == 1:
            parsed = _parse_story_item(item, str(i), force_video=False)
            if parsed:
                items.append(parsed)
        else:
            print(f"[WARN] item[{i}] media_type={media_type} tidak dikenal, coba parse anyway")
            parsed = _parse_story_item(item, str(i))
            if parsed:
                items.append(parsed)

    if not items:
        raise HTTPException(status_code=404, detail="Semua entry tidak valid.")

    return {
        "type": "carousel",
        "items": items,
        "description": f"Story by {username}",
        "author": username,
    }


def clean_story_url(url: str) -> str:
    match = re.match(
        r"(https?://(?:www\.)?instagram\.com/stories/[\w.]+)/\d+", url
    )
    if match:
        return match.group(1) + "/"
    return url


async def extract_stories(url: str) -> dict:
    """Extract Instagram stories — direct API first, yt-dlp as fallback"""
    username = extract_username_from_url(url)
    print(f"[INFO] Mencoba direct API untuk @{username}...")

    try:
        return await extract_stories_direct(username)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WARN] Direct API gagal: {e}, fallback ke yt-dlp...")

    strategies = [
        {
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
            "retries": 5,
            "fragment_retries": 5,
            "sleep_interval": 1,
            "max_sleep_interval": 3,
        },
        {
            "extract_flat": False,
            "ignoreerrors": True,
            "retries": 10,
            "fragment_retries": 10,
            "sleep_interval": 2,
            "max_sleep_interval": 5,
        }
    ]

    last_result = None
    for i, extra_opts in enumerate(strategies):
        ydl_opts = {
            "quiet": False,
            "no_warnings": False,
            "noplaylist": False,
            "cookiefile": COOKIE_FILE,
            **extra_opts
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
                                    print(f"[WARN] Strategi {i+1}, gagal resolve entry: {e}")
                                    continue

                    info["entries"] = resolved_entries

                result = parse_story_response(info)
                print(f"[INFO] Strategi {i+1} berhasil: {len(result['items'])} item")

                if last_result is None or len(result["items"]) > len(last_result["items"]):
                    last_result = result

        except HTTPException:
            raise
        except Exception as e:
            print(f"[WARN] Strategi {i+1} gagal: {e}")
            continue

    if last_result:
        return last_result

    raise HTTPException(status_code=400, detail="Gagal memproses semua strategi")


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
async def get_story(url: str = Query(..., description="Instagram story URL")):
    clean_url = clean_story_url(url)
    print(f"[DEBUG] story request: {url} -> {clean_url}")
    try:
        return JSONResponse(await extract_stories(clean_url))
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
    """Endpoint debug — lihat raw data dari Instagram API (semua host)"""
    user_id = await get_instagram_user_id(username)

    results = {}
    combos = [
        ("i.instagram.com", False),
        ("i.instagram.com", True),
        ("www.instagram.com", True),
    ]

    for host, use_web in combos:
        name = f"{host}_{'web' if use_web else 'app'}"
        headers = get_ig_api_headers(use_web=use_web)
        ep_url = f"https://{host}/api/v1/feed/user/{user_id}/story/"

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(ep_url, headers=headers)
                data = resp.json()

            reel = data.get("reel") or data.get("reels", {}).get(user_id) or {}
            items_raw = reel.get("items", [])

            summary = []
            for i, item in enumerate(items_raw):
                video_versions = item.get("video_versions", [])
                images = item.get("image_versions2", {}).get("candidates", [])
                carousel = item.get("carousel_media", [])

                summary.append({
                    "index": i,
                    "pk": item.get("pk"),
                    "id": item.get("id"),
                    "media_type": item.get("media_type"),
                    "has_video_versions": len(video_versions) > 0,
                    "has_image_versions": len(images) > 0,
                    "has_carousel_media": len(carousel) > 0,
                    "taken_at": item.get("taken_at"),
                })

            results[name] = {
                "status": resp.status_code,
                "total_raw_items": len(items_raw),
                "items": summary,
            }
        except Exception as e:
            results[name] = {"error": str(e)}

    return {
        "user_id": user_id,
        "endpoints": results,
    }


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
