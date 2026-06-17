from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_compress import Compress
import requests
from requests.adapters import HTTPAdapter
import time
import random

app = Flask(__name__)
CORS(app)
Compress(app)  

BASE_API = "https://music-api.albatross0071.workers.dev/api"

# --- TCP Connection Pooling & Cache Headers ---
session = requests.Session()
adapter = HTTPAdapter(
    pool_connections=50,
    pool_maxsize=50,
    max_retries=3
)
session.mount("https://", adapter)
session.mount("http://", adapter)

@app.after_request
def add_cache_headers(response):
    """Automatically applies Render/Browser caching to successful GETs"""
    if request.method == 'GET' and response.status_code == 200:
        if request.path not in ['/health', '/stats']:
            response.headers["Cache-Control"] = "public, max-age=300"
    return response


# --- Globals & Internal Caching ---
search_cache = {}
song_cache = {}
lyrics_cache = {}
recommend_cache = {}
trending_cache = {}
CACHE_TIME = 1800  # 30 mins

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
    if not query: return []
    try:
        r = session.get(f"{BASE_API}/search", params={"query": query}, timeout=15)
        return r.json().get("data", {}).get("songs", {}).get("results", [])
    except:
        return []

def fetch_single_song_raw(song_id):
    cached = get_cache(song_cache, song_id)
    if cached: return cached

    try:
        r = session.get(f"{BASE_API}/songs/{song_id}", timeout=15)
        r.raise_for_status()
        data = r.json()
        set_cache(song_cache, song_id, data)
        return data
    except:
        return None

def extract_stream_url(song_id):
    song_data = fetch_single_song_raw(song_id)
    if not song_data or "data" not in song_data or not song_data["data"]:
        return None

    urls = song_data["data"][0].get("downloadUrl", [])
    if not urls: return None
    
    quality_order = ["320kbps", "160kbps", "96kbps", "48kbps", "12kbps"]
    for quality in quality_order:
        match = next((x for x in urls if x.get("quality") == quality), None)
        if match:
            return match.get("url")
    return urls[0].get("url")

def generate_recommendations(song_id):
    cached = get_cache(recommend_cache, song_id)
    if cached: return cached

    song_data = fetch_single_song_raw(song_id)
    if not song_data or "data" not in song_data or not song_data["data"]:
        return []
        
    current_song = song_data["data"][0]

    song_name = current_song.get("name", "")
    artist = current_song.get("primaryArtists", "")
    album = current_song.get("album", {})
    album_name = album.get("name", "") if isinstance(album, dict) else ""
    language = current_song.get("language", "")
    language = language.lower() if language else ""

    recommendations = []
    
    if album_name:
        try: recommendations.extend(search_songs(album_name)[:12])
        except: pass
    if artist:
        try: recommendations.extend(search_songs(artist)[:9])
        except: pass
    
    if artist and song_name:
        try: recommendations.extend(search_songs(f"{artist} {song_name}")[:8])
        except: pass
    if song_name:
        try: recommendations.extend(search_songs(song_name)[:8])
        except: pass
    
    if language in TRENDING_MAP:
        try:
            trending_artists = random.sample(TRENDING_MAP[language], 2)
            for ta in trending_artists:
                recommendations.extend(search_songs(ta)[:3])
        except: pass
    elif language:
        try: recommendations.extend(search_songs(language)[:6])
        except: pass
    
    seen_ids = set()
    seen_titles = set()
    final = []
    
    for song in recommendations:
        s_id = song.get("id")
        s_title = song.get("name", "").lower().strip()
        
        if not s_id or s_id == song_id or s_id in seen_ids:
            continue
            
        if s_title in seen_titles and song_name.lower().strip() in s_title:
            continue
            
        seen_ids.add(s_id)
        seen_titles.add(s_title)
        final.append(song)

    result = final[:30]
    set_cache(recommend_cache, song_id, result)
    return result


# --- Base Routes ---
@app.route("/")
def home():
    return jsonify({"status": "online", "name": "Royverse Music API Edge-Optimized"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})

@app.route("/stats")
def stats():
    return jsonify({
        "search_cache": len(search_cache),
        "song_cache": len(song_cache),
        "lyrics_cache": len(lyrics_cache),
        "recommend_cache": len(recommend_cache),
        "trending_cache": len(trending_cache)
    })


# --- Search & Content Routes ---
@app.route("/search")
def search():
    raw_query = request.args.get("q")
    if not raw_query: return jsonify({"error": "query required"}), 400

    query = raw_query.lower().strip()

    cached = get_cache(search_cache, query)
    if cached: return jsonify(cached)

    try:
        r = session.get(f"{BASE_API}/search", params={"query": query}, timeout=15)
        data = r.json()
        set_cache(search_cache, query, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/suggest")
def suggest():
    raw_query = request.args.get("q")
    if not raw_query: return jsonify([])
    
    query = raw_query.lower().strip()

    try:
        songs = search_songs(query)
        suggestions = []
        seen = set()

        for song in songs[:10]:
            title = (song.get("title") or song.get("name", "")).replace("&quot;", '"').replace("&amp;", "&")
            artist = (song.get("primaryArtists") or song.get("artists", "")).replace("&quot;", '"').replace("&amp;", "&")
            
            dedupe_key = f"{title}_{artist}".lower()
            if dedupe_key not in seen and title:
                seen.add(dedupe_key)
                suggestions.append({
                    "id": song.get("id"),
                    "title": title,
                    "artist": artist,
                    # Fallback image extraction for suggestions
                    "image": song.get("image")[-1]["url"] if (song.get("image") and isinstance(song.get("image"), list)) else ""
                })

        return jsonify(suggestions)
    except Exception:
        return jsonify([])

@app.route("/artist/<name>")
def artist_songs(name):
    if not name: return jsonify({"artist": "", "songs": []})
    
    query = name.lower().strip()
    try:
        # Trusting the primary search engine results for the artist 
        # avoids strict string matching issues that cause empty pages.
        raw_results = search_songs(query)
                
        return jsonify({
            "artist": name,
            "songs": raw_results[:50]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/trending")
def trending():
    cached = get_cache(trending_cache, "global_trending")
    if cached: return jsonify(cached)

    try:
        # Fetching a solid mix of globally trending artists to populate homescreen
        queries = ["The Weeknd", "Arijit Singh", "Ed Sheeran", "Pritam", "Imagine Dragons"]
        results = []
        for q in queries:
            songs = search_songs(q)
            if songs: results.extend(songs[:3])
            
        random.shuffle(results)
        set_cache(trending_cache, "global_trending", {"songs": results[:15]})
        return jsonify({"songs": results[:15]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Playback & Queue Management ---
@app.route("/song/<song_id>")
def song(song_id):
    data = fetch_single_song_raw(song_id)
    if data:
        return jsonify(data)
    return jsonify({"error": "Song not found"}), 404

@app.route("/stream/<song_id>")
def stream(song_id):
    try:
        stream_url = extract_stream_url(song_id)
        
        if not stream_url:
            return jsonify({"error": "Stream unavailable"}), 404
            
        cached_song = get_cache(song_cache, song_id)
        title = "Unknown"
        if cached_song and "data" in cached_song and cached_song["data"]:
            title = cached_song["data"][0].get("name", "Unknown")

        return jsonify({
            "id": song_id,
            "title": title,
            "stream": stream_url
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/queue/<song_id>")
def get_queue(song_id):
    try:
        song_data = fetch_single_song_raw(song_id)
        current_song = song_data["data"][0] if (song_data and "data" in song_data) else None
        queue_pool = generate_recommendations(song_id).copy()

        return jsonify({
            "current": current_song,
            "queue": queue_pool
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/preload/<song_id>")
def preload(song_id):
    try:
        current_stream = extract_stream_url(song_id)
        queue = generate_recommendations(song_id).copy()
        next_song = queue.pop(0) if queue else None

        next_stream = None
        if next_song:
            next_stream = extract_stream_url(next_song["id"])

        return jsonify({
            "stream": current_stream,
            "next_stream": next_stream,
            "queue": queue
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Data Infrastructure ---
@app.route("/songs/bulk")
def bulk_songs():
    raw_ids = request.args.get("ids")
    if not raw_ids: return jsonify([])

    id_list = [i.strip() for i in raw_ids.split(",") if i.strip()]
    results = []

    try:
        r = session.get(f"{BASE_API}/songs/{','.join(id_list)}", timeout=12)
        response_json = r.json()
        
        if response_json.get("success") and isinstance(response_json.get("data"), list):
            songs_data = response_json["data"]
        else:
            raise ValueError("Fallback triggered")
            
    except Exception:
        songs_data = []
        for song_id in id_list:
            single_res = fetch_single_song_raw(song_id)
            if single_res and "data" in single_res and single_res["data"]:
                songs_data.append(single_res["data"][0])

    for song in songs_data:
        results.append({
            "id": song.get("id"),
            "title": song.get("name"),
            "artist": song.get("primaryArtists", "Unknown"),
            "image": song.get("image")[-1]["url"] if (song.get("image") and isinstance(song["image"], list)) else None
        })
        
    return jsonify(results)


@app.route("/lyrics")
def lyrics():
    track = request.args.get("track")
    artist = request.args.get("artist")
    if not track: return jsonify({"success": False})

    cache_key = f"{track}_{artist}".lower()
    cached = get_cache(lyrics_cache, cache_key)
    if cached: return jsonify(cached)

    try:
        r = session.get(
            "https://lrclib.net/api/get",
            params={"track_name": track, "artist_name": artist},
            timeout=10
        )
        if r.status_code == 404:
            return jsonify({"success": False, "error": "Lyrics not found"})
            
        data = r.json()
        set_cache(lyrics_cache, cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
