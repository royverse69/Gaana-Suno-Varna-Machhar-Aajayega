
from flask import Flask, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

@app.route("/")
def home():
    return {
        "status": "Royverse Music API Running"
    }

@app.route("/search")
def search():
    q = request.args.get("q")

    r = requests.get(
        f"https://music-api.albatross0071.workers.dev/api/search?query={q}"
    )

    return r.text

@app.route("/song/<song_id>")
def song(song_id):

    r = requests.get(
        f"https://music-api.albatross0071.workers.dev/api/songs/{song_id}"
    )

    return r.text
