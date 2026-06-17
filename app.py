from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time

app = Flask(__name__)
CORS(app)

BASE_API = "https://music-api.albatross0071.workers.dev/api"

session = requests.Session()

search_cache = {}
song_cache = {}
CACHE_TIME = 600  # 10 mins


def get_cache(cache, key):
    item = cache.get(key)

    if not item:
        return None

    if time.time() - item["time"] > CACHE_TIME:
        del cache[key]
        return None

    return item["data"]


def set_cache(cache, key, data):
    cache[key] = {
        "time": time.time(),
        "data": data
    }


@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "name": "Royverse Music API"
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "timestamp": int(time.time())
    })


@app.route("/search")
def search():

    query = request.args.get("q")

    if not query:
        return jsonify({
            "error": "query required"
        }), 400

    cached = get_cache(search_cache, query)

    if cached:
        return jsonify(cached)

    try:

        r = session.get(
            f"{BASE_API}/search",
            params={"query": query},
            timeout=15
        )

        data = r.json()

        set_cache(search_cache, query, data)

        return jsonify(data)

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/song/<song_id>")
def song(song_id):

    cached = get_cache(song_cache, song_id)

    if cached:
        return jsonify(cached)

    try:

        r = session.get(
            f"{BASE_API}/songs/{song_id}",
            timeout=15
        )

        data = r.json()

        set_cache(song_cache, song_id, data)

        return jsonify(data)

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/stream/<song_id>")
def stream(song_id):

    cached = get_cache(song_cache, song_id)

    if cached:
        song_data = cached

    else:

        r = session.get(
            f"{BASE_API}/songs/{song_id}",
            timeout=15
        )

        song_data = r.json()

        set_cache(song_cache, song_id, song_data)

    try:

        song = song_data["data"][0]

        urls = song["downloadUrl"]

        quality_order = [
            "320kbps",
            "160kbps",
            "96kbps",
            "48kbps",
            "12kbps"
        ]

        stream_url = None

        for quality in quality_order:

            match = next(
                (
                    x for x in urls
                    if x["quality"] == quality
                ),
                None
            )

            if match:
                stream_url = match["url"]
                break

        return jsonify({
            "id": song_id,
            "title": song["name"],
            "stream": stream_url
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/recommend/<artist>")
def recommend(artist):

    try:

        r = session.get(
            f"{BASE_API}/search",
            params={"query": artist},
            timeout=15
        )

        data = r.json()

        songs = (
            data
            .get("data", {})
            .get("songs", {})
            .get("results", [])
        )

        return jsonify({
            "artist": artist,
            "songs": songs[:20]
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/trending")
def trending():

    try:

        queries = [
            "The Weeknd",
            "Imagine Dragons",
            "Taylor Swift",
            "Arijit Singh"
        ]

        results = []

        for q in queries:

            r = session.get(
                f"{BASE_API}/search",
                params={"query": q},
                timeout=15
            )

            data = r.json()

            songs = (
                data
                .get("data", {})
                .get("songs", {})
                .get("results", [])
            )

            results.extend(songs[:5])

        return jsonify({
            "songs": results
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/stats")
def stats():

    return jsonify({
        "search_cache": len(search_cache),
        "song_cache": len(song_cache)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
