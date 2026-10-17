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
