from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_compress import Compress
import requests
from requests.adapters import HTTPAdapter
import time
import random

app = Flask(__name__)
CORS(app)
Compress(app)  # Response Compression Enabled

BASE_API = "https://music-api.albatross0071.workers.dev/api"

# --- TCP Connection Pooling ---
session = requests.Session()
adapter = HTTPAdapter(
    pool_connections=20, 
    pool_maxsize=20,
    max_retries=3
)
session.mount("https://", adapter)
session.mount("http://", adapter)

# --- Globals & Caching ---
search_cache = {}
song_cache = {}
lyrics_cache = {}
recommend_cache = {}  # Recommendation Caching Enabled
CACHE_TIME = 600  # 10 mins

TRENDING_MAP = {
    "english": ["The Weeknd", "Imagine Dragons", "Taylor Swift", "OneRepublic", "Ed Sheeran"],
    "hindi": ["Arijit Singh", "Pritam", "Shreya Ghoshal", "Anirudh Ravichander", "Vishal-Shekhar"],
    "spanish": ["Bad Bunny", "J Balvin", "Rosalía", "Shakira"]
}


# --- Core Helper Functions ---
def get_cache(cache, key):
    item = cache.get(key)
    if not item: return None
    if time.time() - item["time"] > CACHE_TIME:
        del cache[key]
        return None
    return item["data"]

def set_cache(cache, key, data):
    cache[key] = {
        "time": time.time(),
        "data": data
    }

def search_songs(query):
    try:
        r = session.get(f"{BASE_API}/search", params={"query": query}, timeout=15)
        return r.json().get("data", {}).get("songs", {}).get("results", [])
    except:
        return []

def extract_stream_url(song_id):
    """Helper to fetch just the highest quality stream URL"""
    cached = get_cache(song_cache, song_id)
    if cached:
        song_data = cached
    else:
        r = session.get(f"{BASE_API}/songs/{song_id}", timeout=15)
        r.raise_for_status()
        song_data = r.json()
        set_cache(song_cache, song_id, song_data)

    urls = song_data["data"][0]["downloadUrl"]
    quality_order = ["320kbps", "160kbps", "96kbps", "48kbps", "12kbps"]
    
    for quality in quality_order:
        match = next((x for x in urls if x["quality"] == quality), None)
        if match:
            return match["url"]
    return None

def generate_recommendations(song_id):
    """Helper to generate recommendations independent of the Flask route"""
    cached = get_cache(recommend_cache, song_id)
    if cached: return cached

    r = session.get(f"{BASE_API}/songs/{song_id}", timeout=15)
    current_song = r.json()["data"][0]

    artist = current_song["artists"]["primary"][0]["name"]
    album = current_song["album"]["name"]
    language = current_song.get("language", "").lower()

    recommendations = []
    recommendations.extend(search_songs(album)[:12])
    recommendations.extend(search_songs(artist)[:9])
    
    if language in TRENDING_MAP:
        trending_artists = random.sample(TRENDING_MAP[language], 2)
        for ta in trending_artists:
            recommendations.extend(search_songs(ta)[:3])
    else:
        recommendations.extend(search_songs(language)[:6])
    
    seen = set()
    final = []
    for song in recommendations:
        if song["id"] == song_id or song["id"] in seen:
            continue
        seen.add(song["id"])
        final.append(song)

    result = final[:30]
    set_cache(recommend_cache, song_id, result)
    return result


# --- Base Routes ---
@app.route("/")
def home():
    return jsonify({"status": "online", "name": "Royverse Music API"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})

@app.route("/stats")
def stats():
    return jsonify({
        "search_cache": len(search_cache),
        "song_cache": len(song_cache),
        "lyrics_cache": len(lyrics_cache),
        "recommend_cache": len(recommend_cache)
    })


# --- Search & Song Info Routes ---
@app.route("/search")
def search():
    query = request.args.get("q")
    if not query: return jsonify({"error": "query required"}), 400

    cached = get_cache(search_cache, query)
    if cached: return jsonify(cached)

    try:
        r = session.get(f"{BASE_API}/search", params={"query": query}, timeout=15)
        data = r.json()
        set_cache(search_cache, query, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/song/<song_id>")
def song(song_id):
    cached = get_cache(song_cache, song_id)
    if cached: return jsonify(cached)

    try:
        r = session.get(f"{BASE_API}/songs/{song_id}", timeout=15)
        data = r.json()
        set_cache(song_cache, song_id, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stream/<song_id>")
def stream(song_id):
    try:
        stream_url = extract_stream_url(song_id)
        
        # We fetch from cache to get the title quickly since extract_stream_url caches it
        cached_song = get_cache(song_cache, song_id)
        title = cached_song["data"][0]["name"] if cached_song else "Unknown"

        return jsonify({
            "id": song_id,
            "title": title,
            "stream": stream_url
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Enhanced & Preload Features ---
@app.route("/recommend/<song_id>")
def recommend(song_id):
    try:
        data = generate_recommendations(song_id)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/next/<song_id>")
def get_next(song_id):
    try:
        # Fetch current song directly from API/Cache
        cached = get_cache(song_cache, song_id)
        if cached:
            current_song = cached["data"][0]
        else:
            r_current = session.get(f"{BASE_API}/
