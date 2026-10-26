#!/usr/bin/env python3
import http.server
import json
import os
import re
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 45921
HOME = Path("/home/jules")
SPOTUBE_DATA_DIR = HOME / ".local/share/spotube"
OFFLINE_CACHE_DIR = SPOTUBE_DATA_DIR / "offline_cache"
PLAYLIST_CACHE_DIR = OFFLINE_CACHE_DIR / "playlists"
DB_PATH = SPOTUBE_DATA_DIR / "db.sqlite"
PREFS_PATH = SPOTUBE_DATA_DIR / "shared_preferences.json"
NEWPIPE_BIN = SPOTUBE_DATA_DIR / "NewPipeCLI.bin"
DOWNLOAD_DIR = HOME / "Descargas/Spotube"
CACHE_TRACKS_DIR = HOME / ".cache/spotube/cached_tracks"

OFFLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
PLAYLIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TRACKS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

_download_queue = []
_download_lock = threading.Lock()
_downloading_ids = set()
_known_liked_ids = set()


def sanitize_filename(name: str, replacement: str = "") -> str:
    res = re.sub(r'[/?<>\\:*|"]', replacement, name)
    res = re.sub(r"[\x00-\x1f\x80-\x9f]", replacement, res)
    res = re.sub(r"^\.+$", replacement, res)
    res = re.sub(
        r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$",
        replacement,
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(r"[. ]+$", replacement, res)
    return res[:255]


def get_download_path(track: dict) -> Path:
    artists = ", ".join(a["name"] for a in track.get("artists", []) if a.get("name"))
    fname = sanitize_filename(f"{track.get('name', 'Unknown')} - {artists}.m4a")
    return DOWNLOAD_DIR / fname


def get_cache_path(track: dict, video_id: str) -> Path:
    artists = ",".join(a["name"] for a in track.get("artists", []) if a.get("name"))
    fname = sanitize_filename(f"{track.get('name', 'Unknown')} - {artists} ({video_id}).m4a")
    return CACHE_TRACKS_DIR / fname


def check_internet(timeout: float = 2.0) -> bool:
    for host in ("1.1.1.1", "8.8.8.8", "api-partner.spotify.com"):
        try:
            req = urllib.request.Request(
                "https://open.spotify.com/api/server-time"
                if "spotify" in host
                else f"http://{host}",
                method="HEAD",
                headers={"User-Agent": USER_AGENT},
            )
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            continue
    return False


def get_spotify_access_token():
    try:
        if not PREFS_PATH.exists():
            return None
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        for k, v in prefs.items():
            if k.endswith(".credentials") and isinstance(v, str):
                creds = json.loads(v)
                if creds.get("accessToken"):
                    return creds["accessToken"]
    except Exception as e:
        print(f"[token] error reading token: {e}", flush=True)
    return None


def ensure_offline_credentials_valid():
    """Ensure has_local_helper flag is set on Linux and extend credential expiration when offline."""
    try:
        if not PREFS_PATH.exists():
            return
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        changed = False
        if prefs.get("flutter.spotube_plugin.spotify.has_local_helper") is not True:
            prefs["flutter.spotube_plugin.spotify.has_local_helper"] = True
            changed = True
        if not check_internet(timeout=1.5):
            future_ms = int((time.time() + 86400 * 30) * 1000)
            for k, v in list(prefs.items()):
                if k.endswith(".credentials") and isinstance(v, str):
                    creds = json.loads(v)
                    if creds.get("expiration", 0) < future_ms:
                        creds["expiration"] = future_ms
                        prefs[k] = json.dumps(creds)
                        changed = True
        if changed:
            tmp = PREFS_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
            tmp.replace(PREFS_PATH)
    except Exception as e:
        print(f"[creds] error updating offline expiration: {e}", flush=True)


def convert_gql_track(item: dict) -> dict | None:
    try:
        t_wrap = item.get("track") or {}
        uri = t_wrap.get("_uri") or ""
        track_id = uri.split(":")[-1]
        data = t_wrap.get("data") or {}
        if not track_id or data.get("__typename") != "Track":
            return None

        artists = []
        for a in (data.get("artists") or {}).get("items") or []:
            a_uri = a.get("uri") or ""
            a_id = a_uri.split(":")[-1]
            a_name = (a.get("profile") or {}).get("name") or "Unknown"
            artists.append(
                {
                    "id": a_id,
                    "name": a_name,
                    "externalUri": f"https://open.spotify.com/artist/{a_id}",
                    "images": None,
                }
            )

        alb = data.get("albumOfTrack") or {}
        alb_uri = alb.get("uri") or ""
        alb_id = alb_uri.split(":")[-1]
        alb_artists = []
        for a in (alb.get("artists") or {}).get("items") or []:
            a_uri = a.get("uri") or ""
            a_id = a_uri.split(":")[-1]
            a_name = (a.get("profile") or {}).get("name") or "Unknown"
            alb_artists.append(
                {
                    "id": a_id,
                    "name": a_name,
                    "externalUri": f"https://open.spotify.com/artist/{a_id}",
                    "images": None,
                }
            )
        images = ((alb.get("coverArt") or {}).get("sources")) or []

        return {
            "id": track_id,
            "name": data.get("name") or "Unknown",
            "externalUri": f"https://open.spotify.com/track/{track_id}",
            "explicit": (data.get("contentRating") or {}).get("label") == "EXPLICIT",
            "durationMs": (data.get("duration") or {}).get("totalMilliseconds") or 0,
            "isrc": "",
            "artists": artists or [{"id": "unknown", "name": "Unknown", "externalUri": "", "images": None}],
            "album": {
                "id": alb_id or "unknown",
                "name": alb.get("name") or "Unknown Album",
                "externalUri": f"https://open.spotify.com/album/{alb_id}",
                "releaseDate": "1970",
                "releaseDatePrecision": "year",
                "artists": alb_artists or artists,
                "images": images,
                "albumType": "album",
            },
        }
    except Exception as e:
        print(f"[convert] error: {e}", flush=True)
        return None


def fetch_spotify_gql_library_tracks(limit: int = 5000, offset: int = 0) -> list[dict] | None:
    token = get_spotify_access_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            data=json.dumps(
                {
                    "variables": {"offset": offset, "limit": limit},
                    "operationName": "fetchLibraryTracks",
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240",
                        }
                    },
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.load(resp)
        items_raw = raw["data"]["me"]["library"]["tracks"]["items"]
        tracks = []
        for item in items_raw:
            t = convert_gql_track(item)
            if t:
                tracks.append(t)
        return tracks
    except Exception as e:
        print(f"[gql] fetchLibraryTracks error: {e}", flush=True)
        return None


def fetch_spotify_me() -> dict | None:
    token = get_spotify_access_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return {
            "id": data.get("id") or "user",
            "name": data.get("display_name") or "Usuario",
            "images": data.get("images") or [],
            "externalUri": (data.get("external_urls") or {}).get("spotify")
            or f"https://open.spotify.com/user/{data.get('id', '')}",
        }
    except Exception as e:
        print(f"[me] error: {e}", flush=True)
        return None


def load_cached_saved_tracks() -> dict:
    p = OFFLINE_CACHE_DIR / "saved_tracks.json"
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"limit": 0, "nextOffset": None, "hasMore": False, "total": 0, "items": []}


def save_cached_json(name: str, data: dict):
    p = OFFLINE_CACHE_DIR / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(p)


def sync_hardlinks_and_video_map() -> dict[str, Path]:
    """
    Links all downloaded tracks in /home/jules/Descargas/Spotube/*.m4a into
    /home/jules/.cache/spotube/cached_tracks/<TrackName> - <Artists> (<videoId>).m4a
    and returns a mapping of {video_id: downloaded_file_path}.
    """
    video_map: dict[str, Path] = {}
    try:
        saved = load_cached_saved_tracks().get("items") or []
        track_by_id = {t["id"]: t for t in saved if isinstance(t, dict) and t.get("id")}

        if not DB_PATH.exists():
            return video_map

        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
        cur = conn.cursor()
        cur.execute("SELECT track_id, source_info FROM source_match_table")
        rows = cur.fetchall()
        conn.close()

        for track_id, source_info_str in rows:
            try:
                info = json.loads(source_info_str)
                video_id = info.get("id")
                if not video_id:
                    continue

                track = track_by_id.get(track_id)
                if track:
                    dl_path = get_download_path(track)
                    cache_path = get_cache_path(track, video_id)
                    if dl_path.exists() and dl_path.stat().st_size > 10000:
                        video_map[video_id] = dl_path
                        if not cache_path.exists():
                            try:
                                os.link(dl_path, cache_path)
                            except OSError:
                                shutil.copy2(dl_path, cache_path)
                    elif cache_path.exists() and cache_path.stat().st_size > 10000:
                        video_map[video_id] = cache_path
            except Exception:
                continue

        # Also check existing files in CACHE_TRACKS_DIR for (video_id).m4a pattern
        for f in CACHE_TRACKS_DIR.glob("*.m4a"):
            m = re.search(r"\(([A-Za-z0-9_-]{11})\)\.m4a$", f.name)
            if m and f.stat().st_size > 10000:
                vid = m.group(1)
                if vid not in video_map:
                    video_map[vid] = f
    except Exception as e:
        print(f"[sync_links] error: {e}", flush=True)

    return video_map


def notify_user(title: str, body: str):
    try:
        subprocess.Popen(
            ["notify-send", "-a", "Spotube", "-i", "spotube", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def auto_download_track(track: dict):
    track_id = track.get("id")
    if not track_id:
        return
    dl_path = get_download_path(track)
    if dl_path.exists() and dl_path.stat().st_size > 10000:
        sync_hardlinks_and_video_map()
        return

    with _download_lock:
        if track_id in _downloading_ids:
            return
        _downloading_ids.add(track_id)

    try:
        name = track.get("name", "Unknown")
        artists = [a.get("name", "") for a in track.get("artists", []) if a.get("name")]
        artists_str = ", ".join(artists)
        query = f"{name} - {artists_str}"
        print(f"[auto-dl] Starting automatic download for liked track: {query}", flush=True)
        notify_user("Descargando Me gusta ❤️", f"{name} - {artists_str}")

        res = subprocess.run(
            [str(NEWPIPE_BIN), "--search", query, "--content-filters", "videos"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0 or not res.stdout.strip():
            print(f"[auto-dl] Search failed for {query}", flush=True)
            return

        search_items = json.loads(res.stdout)
        if not search_items:
            return

        best = search_items[0]
        url_str = best.get("url") or ""
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_str)
        video_id = m.group(1) if m else best.get("id")
        if not video_id:
            return

        # Fetch stream URL via NewPipeCLI.bin
        st_res = subprocess.run(
            [str(NEWPIPE_BIN), "--streams", video_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if st_res.returncode != 0 or not st_res.stdout.strip():
            return
        st_data = json.loads(st_res.stdout)
        stream_url = None
        for s in st_data.get("audioStreams") or []:
            if s.get("content"):
                stream_url = s["content"]
                break
        if not stream_url:
            for s in st_data.get("videoStreams") or []:
                if s.get("content"):
                    stream_url = s["content"]
                    break
        if not stream_url:
            return

        # Download & convert to m4a with metadata using ffmpeg
        album_name = (track.get("album") or {}).get("name") or ""
        tmp_out = dl_path.with_suffix(".part.m4a")
        cmd = [
            "ffmpeg",
            "-y",
            "-user_agent",
            USER_AGENT,
            "-i",
            stream_url,
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-metadata",
            f"title={name}",
            "-metadata",
            f"artist={artists_str}",
            "-metadata",
            f"album={album_name}",
            str(tmp_out),
        ]
        ff = subprocess.run(cmd, capture_output=True, timeout=180)
        if ff.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size < 10000:
            tmp_out.unlink(missing_ok=True)
            return

        tmp_out.replace(dl_path)

        # Save match into source_match_table in db.sqlite so Spotube knows the videoId
        source_info = {
            "id": video_id,
            "title": best.get("name") or name,
            "artists": [best.get("uploaderName") or (artists[0] if artists else "Unknown")],
            "duration": int(track.get("durationMs", 0) or (best.get("duration", 0) * 1000)),
            "thumbnail": ((track.get("album") or {}).get("images") or [{}])[0].get("url") or "",
            "externalUri": f"https://www.youtube.com/watch?v={video_id}",
        }
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO source_match_table (track_id, source_info, source_type, created_at) VALUES (?, ?, ?, ?)",
            (track_id, json.dumps(source_info), "youtube-audio", int(time.time())),
        )
        conn.commit()
        conn.close()

        cache_path = get_cache_path(track, video_id)
        if not cache_path.exists():
            try:
                os.link(dl_path, cache_path)
            except OSError:
                shutil.copy2(dl_path, cache_path)

        print(f"[auto-dl] Completed download & offline link: {dl_path.name}", flush=True)
        notify_user("Guardada en Me gusta (Offline) ✅", f"{name} - {artists_str}")
    except Exception as e:
        print(f"[auto-dl] Error downloading {track.get('name')}: {e}", flush=True)
    finally:
        with _download_lock:
            _downloading_ids.discard(track_id)


def prepend_track_to_offline_cache(track: dict):
    saved = load_cached_saved_tracks()
    items = [t for t in (saved.get("items") or []) if t.get("id") != track.get("id")]
    items.insert(0, track)
    saved["items"] = items
    saved["total"] = len(items)
    saved["limit"] = len(items)
    saved["hasMore"] = False
    saved["nextOffset"] = None
    save_cached_json("saved_tracks", saved)


class OfflineHelperHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path.startswith("/stream/"):
            vid = self.path.split("/stream/")[1].replace(".m4a", "").split("?")[0]
            vmap = sync_hardlinks_and_video_map()
            fpath = vmap.get(vid)
            if fpath and fpath.exists():
                size = fpath.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "audio/m4a")
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            online = check_internet(timeout=1.5)
            if not online:
                ensure_offline_credentials_valid()
            self._send_json({"online": online})
            return

        if self.path.startswith("/local-stream/"):
            vid = self.path.split("/local-stream/")[1].split("?")[0]
            vmap = sync_hardlinks_and_video_map()
            fpath = vmap.get(vid)
            online = check_internet(timeout=1.2)
            if (fpath and fpath.exists()) or not online:
                size = fpath.stat().st_size if (fpath and fpath.exists()) else 3000000
                resp = {
                    "dislikeCount": -1,
                    "description": {"type": 1, "content": ""},
                    "likeCount": 1000,
                    "uploaderVerified": True,
                    "dashMpdUrl": "",
                    "subChannelName": "",
                    "startPosition": 0,
                    "duration": 200,
                    "ageLimit": 0,
                    "streamType": "VIDEO_STREAM",
                    "subChannelUrl": "",
                    "host": "",
                    "textualUploadDate": "2024-01-01T00:00:00Z",
                    "id": vid,
                    "viewCount": 10000,
                    "hlsUrl": "",
                    "licence": "YouTube licence",
                    "relatedItems": [],
                    "subChannelAvatars": [],
                    "originalUrl": f"https://www.youtube.com/watch?v={vid}",
                    "uploaderUrl": "https://www.youtube.com",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "tags": [],
                    "shortFormContent": False,
                    "uploadDate": {"isApproximation": False, "offsetDateTime": 1704067200},
                    "uploaderAvatars": [],
                    "uploaderName": "Offline Cache",
                    "videoStreams": [
                        {
                            "itag": 18,
                            "deliveryMethod": "PROGRESSIVE_HTTP",
                            "itagItem": {
                                "audioTrackId": None,
                                "avgBitrate": -1,
                                "audioTrackName": None,
                                "targetDurationSec": -1,
                                "fps": 25,
                                "audioTrackType": None,
                                "initStart": -1,
                                "mediaFormat": "MPEG-4",
                                "bitrate": 128000,
                                "initEnd": -1,
                                "itagType": "VIDEO",
                                "resolutionString": "360p",
                                "approxDurationMs": 200000,
                                "sampleRate": 44100,
                                "indexStart": -1,
                                "quality": "medium",
                                "codec": "avc1.42001E, mp4a.40.2",
                                "audioChannels": 2,
                                "width": 360,
                                "contentLength": size,
                                "id": 18,
                                "indexEnd": -1,
                                "height": 360,
                            },
                            "fps": 25,
                            "initStart": -1,
                            "mediaFormat": "MPEG-4",
                            "bitrate": 128000,
                            "initEnd": -1,
                            "resolution": "360p",
                            "content": f"http://127.0.0.1:{PORT}/stream/{vid}.m4a",
                            "indexStart": -1,
                            "quality": "medium",
                            "codec": "avc1.42001E, mp4a.40.2",
                            "isVideoOnly": False,
                            "manifestUrl": None,
                            "isUrl": True,
                            "width": 360,
                            "id": "18",
                            "indexEnd": -1,
                            "height": 360,
                        }
                    ],
                    "name": vid,
                    "audioStreams": [],
                    "thumbnails": [],
                    "category": "Music",
                    "videoOnlyStreams": [],
                    "uploaderSubscriberCount": 0,
                    "supportInfo": "",
                }
                self._send_json(resp, 200)
            else:
                self._send_json({"cached": False}, 404)
            return

        if self.path.startswith("/stream/"):
            vid = self.path.split("/stream/")[1].replace(".m4a", "").split("?")[0]
            vmap = sync_hardlinks_and_video_map()
            fpath = vmap.get(vid)
            if not fpath or not fpath.exists():
                self.send_response(404)
                self.end_headers()
                return
            size = fpath.stat().st_size
            range_header = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if range_header and range_header.startswith("bytes="):
                parts = range_header.replace("bytes=", "").split("-")
                if parts[0]:
                    start = int(parts[0])
                if len(parts) > 1 and parts[1]:
                    end = int(parts[1])
                status = 206

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "audio/m4a")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(fpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        if self.path.startswith("/cache/"):
            key = self.path.split("/cache/")[1].split("?")[0]
            p = OFFLINE_CACHE_DIR / f"{key}.json"
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    self._send_json(json.load(f))
                return
            if key == "me":
                self._send_json(
                    {
                        "id": "offline",
                        "name": "Jules (Modo Offline)",
                        "images": [],
                        "externalUri": "https://open.spotify.com",
                    }
                )
                return
            self._send_json({"limit": 0, "nextOffset": None, "hasMore": False, "total": 0, "items": []})
            return

        self._send_json({"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}

        if self.path.startswith("/cache/"):
            key = self.path.split("/cache/")[1].split("?")[0]
            save_cached_json(key, data)
            if key == "saved_tracks":
                for t in data.get("items") or []:
                    if isinstance(t, dict) and t.get("id"):
                        _known_liked_ids.add(t["id"])
                threading.Thread(target=sync_hardlinks_and_video_map, daemon=True).start()
            self._send_json({"saved": True})
            return

        if self.path == "/like-and-download":
            if isinstance(data, dict) and data.get("id"):
                _known_liked_ids.add(data["id"])
                prepend_track_to_offline_cache(data)
                threading.Thread(target=auto_download_track, args=(data,), daemon=True).start()
            self._send_json({"queued": True})
            return

        if self.path == "/unlike":
            ids = set(data.get("trackIds") or [])
            saved = load_cached_saved_tracks()
            items = [t for t in (saved.get("items") or []) if t.get("id") not in ids]
            saved["items"] = items
            saved["total"] = len(items)
            saved["limit"] = len(items)
            save_cached_json("saved_tracks", saved)
            self._send_json({"removed": True})
            return

        self._send_json({"ok": True})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def background_sync_loop():
    global _known_liked_ids
    ensure_offline_credentials_valid()
    # 1. Initial populate of saved_tracks.json and me.json if online
    try:
        saved = load_cached_saved_tracks()
        for t in saved.get("items") or []:
            if isinstance(t, dict) and t.get("id"):
                _known_liked_ids.add(t["id"])

        if check_internet(timeout=2.0):
            me_data = fetch_spotify_me()
            if me_data:
                save_cached_json("me", me_data)
            tracks = fetch_spotify_gql_library_tracks(limit=5000, offset=0)
            if tracks:
                for t in tracks:
                    _known_liked_ids.add(t["id"])
                paginated = {
                    "limit": len(tracks),
                    "nextOffset": None,
                    "hasMore": False,
                    "total": len(tracks),
                    "items": tracks,
                }
                save_cached_json("saved_tracks", paginated)
                print(f"[init] Cached {len(tracks)} liked tracks for offline mode.", flush=True)
    except Exception as e:
        print(f"[init] sync error: {e}", flush=True)

    sync_hardlinks_and_video_map()

    # 2. Continuous loop: hard-link new downloads & watch for newly liked songs every 12s
    while True:
        time.sleep(12)
        try:
            sync_hardlinks_and_video_map()
            if not check_internet(timeout=1.5):
                ensure_offline_credentials_valid()
                continue

            # Poll top 10 liked tracks on Spotify to detect newly hearted tracks immediately
            recent_tracks = fetch_spotify_gql_library_tracks(limit=10, offset=0)
            if recent_tracks and _known_liked_ids:
                for t in reversed(recent_tracks):
                    tid = t.get("id")
                    if tid and tid not in _known_liked_ids:
                        _known_liked_ids.add(tid)
                        prepend_track_to_offline_cache(t)
                        threading.Thread(target=auto_download_track, args=(t,), daemon=True).start()
            elif recent_tracks and not _known_liked_ids:
                for t in recent_tracks:
                    if t.get("id"):
                        _known_liked_ids.add(t["id"])
        except Exception as e:
            print(f"[loop] error: {e}", flush=True)


if __name__ == "__main__":
    t = threading.Thread(target=background_sync_loop, daemon=True)
    t.start()
    server = ThreadedHTTPServer(("127.0.0.1", PORT), OfflineHelperHandler)
    print(f"Spotube Offline & Auto-Download Helper listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
