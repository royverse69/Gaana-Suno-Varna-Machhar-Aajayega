"""Mujic/Royverse music backend.

Single browser-facing origin for the Mujic PWA. Catalogue requests are forwarded to a
fixed upstream API; catalogue media is forwarded only for allowlisted Saavn/JioSaavn
CDN hosts. This is not an arbitrary URL proxy.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request
from flask_compress import Compress
from flask_cors import CORS
from requests.adapters import HTTPAdapter

# Keep the worker/proxy provider that the existing backend already used. If the provider
# changes, set UPSTREAM_CATALOGUE_API in Render rather than editing application logic.
BASE_API = os.environ.get(
    "UPSTREAM_CATALOGUE_API",
    "https://music-api.albatross0071.workers.dev/api",
).rstrip("/")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "1800"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "18"))
MAX_BULK_IDS = 50

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
CORS(app, resources={r"/*": {"origins": "*"}})
Compress(app)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mujic-backend")

# Shared connection pool helps reduce connection setup overhead on Render.
session = requests.Session()
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({"User-Agent": "Mujic-Render-Backend/1.0", "Accept": "application/json"})

search_cache: dict = {}
song_cache: dict = {}
lyrics_cache: dict = {}
recommend_cache: dict = {}
trending_cache: dict = {}

TRENDING_MAP = {
    "english": ["The Weeknd", "Imagine Dragons", "Taylor Swift", "OneRepublic", "Ed Sheeran"],
    "hindi": ["Arijit Singh", "Pritam", "Shreya Ghoshal", "Anirudh Ravichander", "Vishal-Shekhar"],
    "spanish": ["Bad Bunny", "J Balvin", "Rosalía", "Shakira"],
}

CATALOGUE_PREFIXES = {"search", "songs", "albums", "artists", "playlists"}
MEDIA_DOMAINS = ("saavncdn.com", "jiosaavn.com")
LYRICSPLUS_ORIGINS = (
    "https://lyricsplus.prjktla.my.id",
    "https://lyricsplus.prjktla.workers.dev",
    "https://lyricsplus-seven.vercel.app",
)


def get_cache(cache: dict, key: str):
    item = cache.get(key)
    if not item:
        return None
    if time.time() - item["time"] > CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return item["data"]


def set_cache(cache: dict, key: str, data):
    cache[key] = {"time": time.time(), "data": data}


def _json_or_body(upstream: requests.Response):
    content_type = upstream.headers.get("Content-Type", "application/json")
    if "json" in content_type.lower():
        try:
            response = jsonify(upstream.json())
            response.status_code = upstream.status_code
            return response
        except ValueError:
            pass
    return Response(upstream.content, status=upstream.status_code, content_type=content_type)


def _upstream_json(url: str, *, params=None, timeout=None):
    """Issue a request to a fixed configured upstream and return decoded JSON."""
    response = session.get(url, params=params, timeout=timeout or REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _extract_search_songs(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    songs = data.get("songs") or {}
    results = songs.get("results") if isinstance(songs, dict) else None
    if isinstance(results, list):
        return results
    # Also tolerate the dedicated /search/songs response shape.
    results = data.get("results")
    return results if isinstance(results, list) else []


def search_songs(query: str):
    query = (query or "").strip()
    if not query:
        return []
    cache_key = query.casefold()
    cached = get_cache(search_cache, cache_key)
    if cached is not None:
        return _extract_search_songs(cached)
    try:
        payload = _upstream_json(f"{BASE_API}/search", params={"query": query})
        set_cache(search_cache, cache_key, payload)
        return _extract_search_songs(payload)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Song search failed: %s", exc)
        return []


def fetch_single_song_raw(song_id: str):
    song_id = (song_id or "").strip()
    if not song_id or len(song_id) > 200:
        return None
    cached = get_cache(song_cache, song_id)
    if cached is not None:
        return cached
    try:
        payload = _upstream_json(f"{BASE_API}/songs/{song_id}")
        if isinstance(payload, dict) and payload.get("success") and payload.get("data"):
            set_cache(song_cache, song_id, payload)
        return payload
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Song detail lookup failed for %s: %s", song_id, exc)
        return None


def _first_song(song_payload):
    data = song_payload.get("data") if isinstance(song_payload, dict) else None
    return data[0] if isinstance(data, list) and data else None


def extract_stream_url(song_id: str):
    payload = fetch_single_song_raw(song_id)
    song = _first_song(payload)
    if not song:
        return None
    urls = song.get("downloadUrl") or []
    if not isinstance(urls, list) or not urls:
        return None
    quality_order = ("320kbps", "160kbps", "96kbps", "48kbps", "12kbps")
    for quality in quality_order:
        item = next((x for x in urls if isinstance(x, dict) and x.get("quality") == quality), None)
        if item and item.get("url"):
            return item["url"]
    return next((x.get("url") for x in urls if isinstance(x, dict) and x.get("url")), None)


def _normal_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _song_title(song):
    return song.get("name") or song.get("title") or ""


def _song_artist(song):
    primary = song.get("primaryArtists")
    if isinstance(primary, str):
        return primary
    artists = song.get("artists") or {}
    if isinstance(artists, dict):
        entries = artists.get("primary") or artists.get("all") or []
        if isinstance(entries, list):
            return ", ".join(str(a.get("name", "")) for a in entries if isinstance(a, dict) and a.get("name"))
    return str(song.get("singers") or "")


def _extract_suggestions(song_id: str):
    """Prefer the API's dedicated similar-song route; gracefully fall back if absent."""
    try:
        payload = _upstream_json(f"{BASE_API}/songs/{song_id}/suggestions", params={"limit": 30})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data:
            return data
    except (requests.RequestException, ValueError) as exc:
        logger.info("Dedicated suggestions unavailable for %s: %s", song_id, exc)
    return []


def generate_recommendations(song_id: str):
    cached = get_cache(recommend_cache, song_id)
    if cached is not None:
        return cached

    current_payload = fetch_single_song_raw(song_id)
    current = _first_song(current_payload)
    if not current:
        return []

    recommendations = _extract_suggestions(song_id)
    # If the official similar-song route is unavailable, preserve the previous backend's
    # album/artist/title/language discovery as a compatibility fallback.
    if not recommendations:
        title = _song_title(current)
        artist = _song_artist(current)
        album = current.get("album") or {}
        album_name = album.get("name", "") if isinstance(album, dict) else str(album or "")
        language = str(current.get("language") or "").casefold()
        if album_name:
            recommendations.extend(search_songs(album_name)[:12])
        if artist:
            recommendations.extend(search_songs(artist)[:9])
        if artist and title:
            recommendations.extend(search_songs(f"{artist} {title}")[:8])
        if title:
            recommendations.extend(search_songs(title)[:8])
        if language in TRENDING_MAP:
            for trending_artist in random.sample(TRENDING_MAP[language], min(2, len(TRENDING_MAP[language]))):
                recommendations.extend(search_songs(trending_artist)[:3])
        elif language:
            recommendations.extend(search_songs(language)[:6])

    current_title = _normal_text(_song_title(current))
    current_artist = _normal_text(_song_artist(current))
    seen_ids = {str(song_id)}
    seen_fingerprints = set()
    final = []
    for song in recommendations:
        if not isinstance(song, dict):
            continue
        sid = str(song.get("id") or "").strip()
        title_key = _normal_text(_song_title(song))
        artist_key = _normal_text(_song_artist(song))
        if not sid or sid in seen_ids or not title_key:
            continue
        if title_key == current_title and (not artist_key or not current_artist or artist_key == current_artist):
            continue
        fingerprint = (title_key, artist_key)
        if fingerprint in seen_fingerprints:
            continue
        seen_ids.add(sid)
        seen_fingerprints.add(fingerprint)
        final.append(song)
        if len(final) >= 30:
            break

    set_cache(recommend_cache, song_id, final)
    return final


# --- App/cache headers ---
@app.after_request
def add_cache_headers(response):
    if request.method == "GET" and response.status_code == 200:
        if request.path not in ("/health", "/stats"):
            response.headers.setdefault("Cache-Control", "public, max-age=300")
    elif response.status_code >= 400:
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# --- Service routes ---
@app.route("/")
def home():
    return jsonify({"status": "online", "name": "Royverse Music API gana sun le lore api kya check akr rha 😁· Mujic backend", "version": "2.0.0"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


@app.route("/stats")
def stats():
    return jsonify({
        "search_cache": len(search_cache), "song_cache": len(song_cache),
        "lyrics_cache": len(lyrics_cache), "recommend_cache": len(recommend_cache),
        "trending_cache": len(trending_cache),
    })


# --- Main API bridge used by the current Mujic frontend ---
@app.route("/api/<path:upstream_path>", methods=["GET"])
def catalogue_api_proxy(upstream_path):
    parts = (upstream_path or "").split("/")
    first = parts[0].casefold() if parts else ""
    if first not in CATALOGUE_PREFIXES or not parts or any(
        not part or part in (".", "..") or "\\" in part or not re.fullmatch(r"[A-Za-z0-9._~-]+", part)
        for part in parts
    ):
        return jsonify({"success": False, "error": "Catalogue route not allowed"}), 404
    target = f"{BASE_API}/{'/'.join(parts)}"
    try:
        upstream = session.get(
            target,
            params=request.args.to_dict(flat=False),
            headers={"Accept": request.headers.get("Accept", "application/json")},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        # Catalogue requests should be JSON. Do not follow an off-host redirect.
        if 300 <= upstream.status_code < 400:
            upstream.close()
            return jsonify({"success": False, "error": "Upstream catalogue returned a redirect"}), 502
        return _json_or_body(upstream)
    except requests.RequestException as exc:
        logger.warning("Catalogue proxy failed for %s: %s", upstream_path, exc)
        return jsonify({"success": False, "error": "Upstream catalogue temporarily unavailable"}), 502


# --- Saavn/JioSaavn media proxy (audio and artwork, including Range requests) ---
def _allowed_media_url(value):
    try:
        parsed = urlparse(str(value or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        return any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS)
    except (TypeError, ValueError):
        return False


@app.route("/media", methods=["GET"])
def catalogue_media_proxy():
    target = request.args.get("url", "").strip()
    if not _allowed_media_url(target):
        return jsonify({"error": "Unsupported catalogue media host"}), 403

    forwarded = {"Accept-Encoding": "identity"}
    for name in ("Range", "If-Range", "If-None-Match", "If-Modified-Since", "Accept"):
        if request.headers.get(name):
            forwarded[name] = request.headers[name]

    upstream = None
    current_target = target
    for _ in range(5):
        if not _allowed_media_url(current_target):
            if upstream is not None:
                upstream.close()
            return jsonify({"error": "Redirected media host is not allowed"}), 403
        try:
            upstream = session.get(
                current_target, headers=forwarded, stream=True, allow_redirects=False,
                timeout=(8, 45),
            )
        except requests.RequestException as exc:
            logger.warning("Media proxy failed: %s", exc)
            return jsonify({"error": "Catalogue media is temporarily unavailable"}), 502
        if upstream.status_code not in (301, 302, 303, 307, 308):
            break
        location = upstream.headers.get("Location")
        upstream.close()
        if not location:
            return jsonify({"error": "Media redirect did not provide a destination"}), 502
        current_target = urljoin(current_target, location)
    else:
        if upstream is not None:
            upstream.close()
        return jsonify({"error": "Too many media redirects"}), 502

    headers = {
        name: upstream.headers[name]
        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified")
        if name in upstream.headers
    }
    headers["Cache-Control"] = "public, max-age=900"
    response = Response(upstream.iter_content(chunk_size=64 * 1024), status=upstream.status_code, headers=headers)
    response.call_on_close(upstream.close)
    return response


# --- Fixed-origin lyrics provider routes; no arbitrary proxy destinations accepted ---
def _fixed_lyrics_request(url, params, timeout=8):
    try:
        upstream = session.get(url, params=params, headers={"Accept": "application/json"}, timeout=timeout)
        return _json_or_body(upstream)
    except requests.RequestException as exc:
        logger.warning("Lyrics proxy failed: %s", exc)
        return jsonify({"success": False, "error": "Lyrics provider temporarily unavailable"}), 502


@app.route("/lyrics/search", methods=["GET"])
def lyrics_search_proxy():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    params = request.args.to_dict(flat=False)
    return _fixed_lyrics_request("https://lrclib.net/api/search", params)


@app.route("/lyrics/get", methods=["GET"])
def lyrics_get_proxy():
    if not request.args.get("track_name", "").strip():
        return jsonify({"error": "track_name required"}), 400
    return _fixed_lyrics_request("https://lrclib.net/api/get", request.args.to_dict(flat=False))


@app.route("/lyricsplus/v2/lyrics/get", methods=["GET"])
def lyricsplus_get_proxy():
    params = request.args.to_dict(flat=False)
    last_response = None
    for origin in LYRICSPLUS_ORIGINS:
        try:
            upstream = session.get(
                f"{origin}/v2/lyrics/get", params=params,
                headers={"Accept": "application/json"}, timeout=6,
            )
            last_response = upstream
            if upstream.status_code in (404, 405, 410) or upstream.status_code >= 500:
                continue
            return _json_or_body(upstream)
        except requests.RequestException:
            continue
    if last_response is not None:
        return _json_or_body(last_response)
    return jsonify({"success": False, "error": "Lyrics providers temporarily unavailable"}), 502


# --- Legacy routes retained for older clients and fallback behavior ---
@app.route("/search", methods=["GET"])
def legacy_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    key = query.casefold()
    cached = get_cache(search_cache, f"global:{key}")
    if cached is not None:
        return jsonify(cached)
    try:
        payload = _upstream_json(f"{BASE_API}/search", params={"query": query})
        set_cache(search_cache, f"global:{key}", payload)
        return jsonify(payload)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Legacy global search failed: %s", exc)
        return jsonify({"error": "Search temporarily unavailable"}), 502


@app.route("/suggest", methods=["GET"])
def suggest():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    suggestions = []
    seen = set()
    for song in search_songs(query)[:20]:
        title = str(song.get("title") or song.get("name") or "").strip()
        artist = _song_artist(song)
        fingerprint = (_normal_text(title), _normal_text(artist))
        if title and fingerprint not in seen:
            seen.add(fingerprint)
            suggestions.append({"id": song.get("id"), "title": title, "artist": artist, "image": _best_image(song.get("image"))})
        if len(suggestions) >= 10:
            break
    return jsonify(suggestions)


def _best_image(image):
    if isinstance(image, list):
        for item in reversed(image):
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    if isinstance(image, dict):
        return image.get("url") or ""
    if isinstance(image, str):
        return image
    return ""


@app.route("/artist/<path:name>", methods=["GET"])
def artist_songs(name):
    artist_name = (name or "").strip()
    if not artist_name:
        return jsonify({"artist": "", "songs": []})
    songs = search_songs(artist_name)
    target = _normal_text(artist_name)
    matched = []
    for song in songs:
        if target and target in _normal_text(_song_artist(song)):
            matched.append(song)
    return jsonify({"artist": artist_name, "songs": matched[:50]})


@app.route("/trending", methods=["GET"])
def trending():
    cached = get_cache(trending_cache, "global_trending")
    if cached is not None:
        return jsonify(cached)
    queries = ("The Weeknd", "Arijit Singh", "Ed Sheeran", "Pritam", "Imagine Dragons")
    results = []
    seen = set()
    for query in queries:
        for song in search_songs(query)[:5]:
            sid = str(song.get("id") or "")
            if sid and sid not in seen:
                seen.add(sid)
                results.append(song)
    payload = {"songs": results[:20]}
    set_cache(trending_cache, "global_trending", payload)
    return jsonify(payload)


@app.route("/song/<song_id>", methods=["GET"])
def legacy_song(song_id):
    payload = fetch_single_song_raw(song_id)
    if payload and _first_song(payload):
        return jsonify(payload)
    return jsonify({"error": "Song not found"}), 404


@app.route("/stream/<song_id>", methods=["GET"])
def legacy_stream(song_id):
    stream_url = extract_stream_url(song_id)
    if not stream_url:
        return jsonify({"error": "Stream unavailable"}), 404
    current = _first_song(fetch_single_song_raw(song_id)) or {}
    return jsonify({"id": song_id, "title": _song_title(current) or "Unknown", "stream": stream_url})


@app.route("/queue/<song_id>", methods=["GET"])
def legacy_queue(song_id):
    payload = fetch_single_song_raw(song_id)
    current = _first_song(payload)
    if not current:
        return jsonify({"error": "Song not found"}), 404
    return jsonify({"current": current, "queue": generate_recommendations(song_id)})


@app.route("/preload/<song_id>", methods=["GET"])
def legacy_preload(song_id):
    current_stream = extract_stream_url(song_id)
    queue = generate_recommendations(song_id)
    next_song = queue[0] if queue else None
    next_stream = extract_stream_url(str(next_song.get("id"))) if next_song and next_song.get("id") else None
    return jsonify({"stream": current_stream, "next_stream": next_stream, "queue": queue[1:] if queue else []})


@app.route("/songs/bulk", methods=["GET"])
def bulk_songs():
    raw_ids = request.args.get("ids", "")
    ids = [part.strip() for part in raw_ids.split(",") if part.strip()][:MAX_BULK_IDS]
    if not ids:
        return jsonify([])
    songs = []
    try:
        payload = _upstream_json(f"{BASE_API}/songs", params={"ids": ",".join(ids)}, timeout=REQUEST_TIMEOUT_SECONDS)
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            songs = payload["data"]
    except (requests.RequestException, ValueError):
        songs = []
    if not songs:
        for sid in ids:
            payload = fetch_single_song_raw(sid)
            item = _first_song(payload)
            if item:
                songs.append(item)
    return jsonify([
        {"id": song.get("id"), "title": _song_title(song), "artist": _song_artist(song), "image": _best_image(song.get("image"))}
        for song in songs
    ])


@app.route("/recommendations/bulk", methods=["GET"])
def bulk_recommendations():
    ids = [part.strip() for part in request.args.get("ids", "").split(",") if part.strip()][:3]
    all_recs = []
    seen_ids = set()
    seen_fingerprints = set()
    for sid in ids:
        for song in generate_recommendations(sid):
            song_id = str(song.get("id") or "")
            fingerprint = (_normal_text(_song_title(song)), _normal_text(_song_artist(song)))
            if not song_id or song_id in seen_ids or fingerprint in seen_fingerprints:
                continue
            seen_ids.add(song_id)
            seen_fingerprints.add(fingerprint)
            all_recs.append(song)
            if len(all_recs) >= 50:
                return jsonify({"recommendations": all_recs})
    return jsonify({"recommendations": all_recs})


@app.route("/lyrics", methods=["GET"])
def legacy_lyrics():
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    if not track:
        return jsonify({"success": False, "error": "track required"}), 400
    key = f"{track}_{artist}".casefold()
    cached = get_cache(lyrics_cache, key)
    if cached is not None:
        return jsonify(cached)
    params = {"track_name": track}
    if artist:
        params["artist_name"] = artist
    try:
        upstream = session.get("https://lrclib.net/api/get", params=params, timeout=10)
        if upstream.status_code == 404:
            return jsonify({"success": False, "error": "Lyrics not found"}), 404
        payload = upstream.json()
        set_cache(lyrics_cache, key, payload)
        return jsonify(payload), upstream.status_code
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Legacy lyrics request failed: %s", exc)
        return jsonify({"success": False, "error": "Lyrics provider temporarily unavailable"}), 502


if __name__ == "__main__":
    # Local development only. Render uses the Gunicorn command in Procfile/render.yaml.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
