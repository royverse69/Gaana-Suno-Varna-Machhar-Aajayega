/**
 * Mujic/Royverse music backend: Cloudflare Workers port of the Flask app.py.
 * Same routes, same JSON shapes, same allowlist rules. Not an arbitrary URL proxy.
 */

const UA = "Mujic-Worker-Backend/1.0";
const MAX_BULK_IDS = 50;
const CATALOGUE_PREFIXES = new Set(["search", "songs", "albums", "artists", "playlists"]);
const MEDIA_DOMAINS = ["saavncdn.com", "jiosaavn.com"];
const LYRICSPLUS_ORIGINS = [
  "https://lyricsplus.prjktla.my.id",
  "https://lyricsplus.prjktla.workers.dev",
  "https://lyricsplus-seven.vercel.app",
];
const TRENDING_MAP = {
  english: ["The Weeknd", "Imagine Dragons", "Taylor Swift", "OneRepublic", "Ed Sheeran"],
  hindi: ["Arijit Singh", "Pritam", "Shreya Ghoshal", "Anirudh Ravichander", "Vishal-Shekhar"],
  spanish: ["Bad Bunny", "J Balvin", "Rosalía", "Shakira"],
};

// ---- config (filled per request from env) ----
let BASE_API = "https://music-api.albatross0071.workers.dev/api";
let CACHE_TTL = 1800;
let TIMEOUT_MS = 18000;
let ALLOWED_ORIGINS = ["https://royverse69.github.io"];
let CATALOGUE_FALLBACK = ""; // optional second catalogue bridge (e.g. Render /api) used only when the first one fails
const FALLBACK_TIMEOUT_MS = 50000; // Render free plan may be cold-starting
let MEDIA_FALLBACK = ""; // optional backend whose /media is used when the Saavn CDN refuses this edge location

// ---- per-isolate memory caches (best effort; Cloudflare edge cache backs the upstream calls) ----
const searchCache = new Map();
const songCache = new Map();
const lyricsCache = new Map();
const recommendCache = new Map();
const trendingCache = new Map();

function getCache(cache, key) {
  const item = cache.get(key);
  if (!item) return null;
  if ((Date.now() - item.time) / 1000 > CACHE_TTL) {
    cache.delete(key);
    return null;
  }
  return item.data;
}
function setCache(cache, key, data) {
  if (cache.size > 500) cache.delete(cache.keys().next().value);
  cache.set(key, { time: Date.now(), data });
}

// ---- helpers ----
const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

function passthrough(up) {
  const h = new Headers();
  h.set("Content-Type", up.headers.get("Content-Type") || "application/json");
  return new Response(up.body, { status: up.status, headers: h });
}

async function fetchJsonOnce(url, params, timeoutMs) {
  const u = new URL(url);
  if (params) for (const [k, v] of Object.entries(params)) u.searchParams.set(k, String(v));
  const res = await fetch(u.toString(), {
    headers: { "User-Agent": UA, Accept: "application/json" },
    signal: AbortSignal.timeout(timeoutMs),
    cf: { cacheEverything: true, cacheTtlByStatus: { "200-299": CACHE_TTL, "300-599": 0 } },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
const apiBroken = (payload) => !payload || typeof payload !== "object" || payload.success === false;

// Primary catalogue first (fast, same edge); if it errors or reports failure, try the fallback bridge once.
async function upstreamJson(url, params, timeoutMs) {
  let payload;
  let err;
  try {
    payload = await fetchJsonOnce(url, params, timeoutMs || TIMEOUT_MS);
    if (!apiBroken(payload)) return payload;
  } catch (e) {
    err = e;
  }
  if (CATALOGUE_FALLBACK && url.startsWith(BASE_API)) {
    try {
      const p2 = await fetchJsonOnce(CATALOGUE_FALLBACK + url.slice(BASE_API.length), params, FALLBACK_TIMEOUT_MS);
      if (!apiBroken(p2)) return p2;
    } catch (e) {
      if (payload === undefined) err = err || e;
    }
  }
  if (payload !== undefined) return payload;
  throw err;
}

const normalText = (v) =>
  String(v ?? "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const songTitle = (s) => s.name || s.title || "";

function songArtist(song) {
  if (typeof song.primaryArtists === "string") return song.primaryArtists;
  const artists = song.artists || {};
  if (artists && typeof artists === "object" && !Array.isArray(artists)) {
    const entries = artists.primary || artists.all || [];
    if (Array.isArray(entries)) {
      return entries
        .filter((a) => a && typeof a === "object" && a.name)
        .map((a) => String(a.name))
        .join(", ");
    }
  }
  return String(song.singers || "");
}

function bestImage(image) {
  if (Array.isArray(image)) {
    for (let i = image.length - 1; i >= 0; i--) {
      if (image[i] && typeof image[i] === "object" && image[i].url) return image[i].url;
    }
  } else if (image && typeof image === "object") {
    return image.url || "";
  } else if (typeof image === "string") {
    return image;
  }
  return "";
}

function extractSearchSongs(payload) {
  if (!payload || typeof payload !== "object") return [];
  const data = payload.data || {};
  const songs = data.songs || {};
  const results = songs && typeof songs === "object" ? songs.results : null;
  if (Array.isArray(results)) return results;
  return Array.isArray(data.results) ? data.results : [];
}

async function searchSongs(query) {
  query = (query || "").trim();
  if (!query) return [];
  const key = query.toLowerCase();
  const cached = getCache(searchCache, key);
  if (cached !== null) return extractSearchSongs(cached);
  try {
    const payload = await upstreamJson(`${BASE_API}/search`, { query });
    setCache(searchCache, key, payload);
    return extractSearchSongs(payload);
  } catch (e) {
    console.warn("Song search failed:", String(e));
    return [];
  }
}

async function fetchSingleSongRaw(songId) {
  songId = (songId || "").trim();
  if (!songId || songId.length > 200) return null;
  const cached = getCache(songCache, songId);
  if (cached !== null) return cached;
  try {
    const payload = await upstreamJson(`${BASE_API}/songs/${encodeURIComponent(songId)}`);
    if (payload && payload.success && payload.data) setCache(songCache, songId, payload);
    return payload;
  } catch (e) {
    console.warn(`Song detail lookup failed for ${songId}:`, String(e));
    return null;
  }
}

function firstSong(payload) {
  const data = payload && typeof payload === "object" ? payload.data : null;
  return Array.isArray(data) && data.length ? data[0] : null;
}

async function extractStreamUrl(songId) {
  const song = firstSong(await fetchSingleSongRaw(songId));
  if (!song) return null;
  const urls = song.downloadUrl || [];
  if (!Array.isArray(urls) || !urls.length) return null;
  for (const q of ["320kbps", "160kbps", "96kbps", "48kbps", "12kbps"]) {
    const item = urls.find((x) => x && typeof x === "object" && x.quality === q);
    if (item && item.url) return item.url;
  }
  const any = urls.find((x) => x && x.url);
  return any ? any.url : null;
}

async function extractSuggestions(songId) {
  try {
    const payload = await upstreamJson(`${BASE_API}/songs/${encodeURIComponent(songId)}/suggestions`, { limit: 30 });
    const data = payload && typeof payload === "object" ? payload.data : null;
    if (Array.isArray(data) && data.length) return data;
  } catch (e) {
    console.info(`Dedicated suggestions unavailable for ${songId}:`, String(e));
  }
  return [];
}

function sample(arr, n) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a.slice(0, n);
}

async function generateRecommendations(songId) {
  const cached = getCache(recommendCache, songId);
  if (cached !== null) return cached;

  const current = firstSong(await fetchSingleSongRaw(songId));
  if (!current) return [];

  let recommendations = await extractSuggestions(songId);
  if (!recommendations.length) {
    const title = songTitle(current);
    const artist = songArtist(current);
    const album = current.album || {};
    const albumName = album && typeof album === "object" ? album.name || "" : String(album || "");
    const language = String(current.language || "").toLowerCase();
    const jobs = [];
    if (albumName) jobs.push(searchSongs(albumName).then((r) => r.slice(0, 12)));
    if (artist) jobs.push(searchSongs(artist).then((r) => r.slice(0, 9)));
    if (artist && title) jobs.push(searchSongs(`${artist} ${title}`).then((r) => r.slice(0, 8)));
    if (title) jobs.push(searchSongs(title).then((r) => r.slice(0, 8)));
    if (TRENDING_MAP[language]) {
      for (const a of sample(TRENDING_MAP[language], Math.min(2, TRENDING_MAP[language].length))) {
        jobs.push(searchSongs(a).then((r) => r.slice(0, 3)));
      }
    } else if (language) {
      jobs.push(searchSongs(language).then((r) => r.slice(0, 6)));
    }
    recommendations = (await Promise.all(jobs)).flat();
  }

  const curTitle = normalText(songTitle(current));
  const curArtist = normalText(songArtist(current));
  const seenIds = new Set([String(songId)]);
  const seenFp = new Set();
  const final = [];
  for (const song of recommendations) {
    if (!song || typeof song !== "object") continue;
    const sid = String(song.id || "").trim();
    const tk = normalText(songTitle(song));
    const ak = normalText(songArtist(song));
    if (!sid || seenIds.has(sid) || !tk) continue;
    if (tk === curTitle && (!ak || !curArtist || ak === curArtist)) continue;
    const fp = `${tk}\u0000${ak}`;
    if (seenFp.has(fp)) continue;
    seenIds.add(sid);
    seenFp.add(fp);
    final.push(song);
    if (final.length >= 30) break;
  }
  setCache(recommendCache, songId, final);
  return final;
}

// ---- media allowlist ----
function allowedMediaUrl(value) {
  try {
    const u = new URL(String(value || ""));
    const host = u.hostname.toLowerCase().replace(/\.$/, "");
    if (u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443")) return false;
    return MEDIA_DOMAINS.some((d) => host === d || host.endsWith("." + d));
  } catch {
    return false;
  }
}

async function mediaProxy(url, request) {
  const target = (url.searchParams.get("url") || "").trim();
  if (!allowedMediaUrl(target)) return json({ error: "Unsupported catalogue media host" }, 403);

  const forwarded = { "Accept-Encoding": "identity", "User-Agent": UA };
  for (const n of ["Range", "If-Range", "If-None-Match", "If-Modified-Since", "Accept"]) {
    const v = request.headers.get(n);
    if (v) forwarded[n] = v;
  }

  let upstream = null;
  let current = target;
  let ok = false;
  for (let i = 0; i < 5; i++) {
    if (!allowedMediaUrl(current)) {
      upstream?.body?.cancel();
      return json({ error: "Redirected media host is not allowed" }, 403);
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000); // header phase only, not the stream
    try {
      upstream = await fetch(current, { headers: forwarded, redirect: "manual", signal: ctrl.signal });
    } catch (e) {
      console.warn("Media proxy failed:", String(e));
      return json({ error: "Catalogue media is temporarily unavailable" }, 502);
    } finally {
      clearTimeout(timer);
    }
    if (![301, 302, 303, 307, 308].includes(upstream.status)) {
      ok = true;
      break;
    }
    const location = upstream.headers.get("Location");
    upstream.body?.cancel();
    if (!location) return json({ error: "Media redirect did not provide a destination" }, 502);
    current = new URL(location, current).toString();
  }
  if (!ok) return json({ error: "Too many media redirects" }, 502);

  // The Saavn CDN can refuse requests that leave from a Cloudflare location outside its allowed region.
  // If that happens, retry this one request through the fallback backend's /media (e.g. Render).
  let via = "edge";
  if ([401, 403, 451].includes(upstream.status) && MEDIA_FALLBACK) {
    const blocked = upstream.status;
    upstream.body?.cancel();
    console.warn(`Saavn CDN returned ${blocked} to this edge; retrying via fallback media backend`);
    const fbHeaders = { Accept: forwarded.Accept || "*/*" };
    for (const n of ["Range", "If-Range", "If-None-Match", "If-Modified-Since"]) {
      if (forwarded[n]) fbHeaders[n] = forwarded[n];
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000); // Render free plan may be cold-starting
    try {
      const fb = await fetch(`${MEDIA_FALLBACK}/media?url=${encodeURIComponent(target)}`, {
        headers: fbHeaders, redirect: "manual", signal: ctrl.signal,
      });
      if (fb.status >= 400) {
        fb.body?.cancel();
        return json({ error: "Catalogue media blocked from this region", upstream_status: blocked }, 502);
      }
      upstream = fb;
      via = "fallback";
    } catch (e) {
      console.warn("Fallback media backend failed:", String(e));
      return json({ error: "Catalogue media blocked from this region", upstream_status: blocked }, 502);
    } finally {
      clearTimeout(timer);
    }
  }

  const headers = new Headers();
  for (const n of ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"]) {
    const v = upstream.headers.get(n);
    if (v) headers.set(n, v);
  }
  headers.set("Cache-Control", "public, max-age=900");
  headers.set("X-Media-Via", via);
  return new Response(upstream.body, { status: upstream.status, headers });
}

// ---- catalogue bridge ----
async function fetchCatalogue(base, parts, search, request, timeoutMs) {
  const up = await fetch(`${base}/${parts.join("/")}${search}`, {
    headers: { "User-Agent": UA, Accept: request.headers.get("Accept") || "application/json" },
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (up.status >= 300 && up.status < 400) {
    up.body?.cancel();
    return { redirect: true, status: 502, type: "application/json", text: JSON.stringify({ success: false, error: "Upstream catalogue returned a redirect" }) };
  }
  return { status: up.status, type: up.headers.get("Content-Type") || "application/json", text: await up.text() };
}

// True when the answer looks like a block/failure rather than real data.
function catalogueBroken(r, parts) {
  if (r.redirect || r.status < 200 || r.status >= 300) return true;
  if (!r.type.toLowerCase().includes("json")) return true;
  let j;
  try { j = JSON.parse(r.text); } catch { return true; }
  if (j && j.success === false) return true;
  const first = (parts[0] || "").toLowerCase();
  if (first !== "search" && !parts.includes("suggestions") && j && typeof j === "object" && "data" in j) {
    const d = j.data;
    if (d == null) return true;
    if (Array.isArray(d) ? d.length === 0 : typeof d === "object" && Object.keys(d).length === 0) return true;
  }
  return false;
}

async function catalogueProxy(url, request) {
  let parts;
  try {
    parts = decodeURIComponent(url.pathname.slice("/api/".length)).split("/");
  } catch {
    parts = [""];
  }
  const first = (parts[0] || "").toLowerCase();
  if (
    !CATALOGUE_PREFIXES.has(first) ||
    parts.some((p) => !p || p === "." || p === ".." || p.includes("\\") || !/^[A-Za-z0-9._~-]+$/.test(p))
  ) {
    return json({ success: false, error: "Catalogue route not allowed" }, 404);
  }

  const reply = (r, via, cacheable) => {
    const headers = { "Content-Type": r.type, "X-Catalogue-Via": via };
    if (!cacheable) headers["X-No-Edge-Cache"] = "1";
    return new Response(r.text, { status: r.status, headers });
  };

  let primary = null;
  try {
    primary = await fetchCatalogue(BASE_API, parts, url.search, request, TIMEOUT_MS);
  } catch (e) {
    console.warn(`Catalogue proxy failed for ${url.pathname}:`, String(e));
  }
  if (primary && !catalogueBroken(primary, parts)) return reply(primary, "edge", true);

  if (CATALOGUE_FALLBACK) {
    try {
      const fb = await fetchCatalogue(CATALOGUE_FALLBACK, parts, url.search, request, FALLBACK_TIMEOUT_MS);
      if (!catalogueBroken(fb, parts)) return reply(fb, "fallback", true);
      console.warn(`Fallback catalogue also failed for ${url.pathname}: HTTP ${fb.status}`);
    } catch (e) {
      console.warn(`Fallback catalogue failed for ${url.pathname}:`, String(e));
    }
  }
  if (primary) return reply(primary, "edge-unverified", false); // return what we got, but never cache it
  return json({ success: false, error: "Upstream catalogue temporarily unavailable" }, 502);
}

// ---- diagnostics: fixed probes only, no user input ----
async function diag(request) {
  const probes = ["search/artists?query=The%20Weeknd", "search/songs?query=The%20Weeknd", "search/songs?query=Arijit%20Singh"];
  const out = {
    colo: request.cf && request.cf.colo, country: request.cf && request.cf.country,
    primary_base: BASE_API, fallback_base: CATALOGUE_FALLBACK || null, probes: {},
  };
  for (const pr of probes) {
    const [pathOnly, qs] = pr.split("?");
    const call = (base, tmo) => {
      const parts = pathOnly.split("/");
      const t0 = Date.now();
      return fetch(`${base}/${pr}`, { headers: { "User-Agent": UA, Accept: "application/json" }, redirect: "manual", signal: AbortSignal.timeout(tmo) })
        .then(async (res) => {
          const text = await res.text();
          let j = null;
          try { j = JSON.parse(text); } catch {}
          const d = j && j.data;
          return { status: res.status, ms: Date.now() - t0, success: j ? j.success : null,
            count: d && Array.isArray(d.results) ? d.results.length : Array.isArray(d) ? d.length : null, sample: text.slice(0, 120) };
        })
        .catch((e) => ({ error: String(e), ms: Date.now() - t0 }));
    };
    out.probes[pr] = {
      primary: await call(BASE_API, TIMEOUT_MS),
      fallback: CATALOGUE_FALLBACK ? await call(CATALOGUE_FALLBACK, FALLBACK_TIMEOUT_MS) : null,
    };
  }
  const r = json(out);
  r.headers.set("Cache-Control", "no-store");
  return r;
}

// ---- lyrics ----
async function fixedLyricsRequest(base, search, timeoutMs = 8000) {
  try {
    const up = await fetch(base + search, {
      headers: { "User-Agent": UA, Accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    return passthrough(up);
  } catch (e) {
    console.warn("Lyrics proxy failed:", String(e));
    return json({ success: false, error: "Lyrics provider temporarily unavailable" }, 502);
  }
}

async function lyricsPlus(url) {
  let last = null;
  for (const origin of LYRICSPLUS_ORIGINS) {
    try {
      const up = await fetch(`${origin}/v2/lyrics/get${url.search}`, {
        headers: { "User-Agent": UA, Accept: "application/json" },
        signal: AbortSignal.timeout(6000),
      });
      if (last) last.body?.cancel();
      last = up;
      if ([404, 405, 410].includes(up.status) || up.status >= 500) continue;
      return passthrough(up);
    } catch {
      continue;
    }
  }
  if (last) return passthrough(last);
  return json({ success: false, error: "Lyrics providers temporarily unavailable" }, 502);
}

async function lyricsOvh(url) {
  const title = (url.searchParams.get("track_name") || "").trim();
  const artist = (url.searchParams.get("artist_name") || "").trim();
  if (!title || !artist) return json({ success: false, error: "track_name and artist_name required" }, 400);
  const key = `lyrics-ovh:${artist}:${title}`.toLowerCase();
  const cached = getCache(lyricsCache, key);
  if (cached) return json(cached);
  try {
    const up = await fetch(
      `https://api.lyrics.ovh/v1/${encodeURIComponent(artist)}/${encodeURIComponent(title)}`,
      { headers: { "User-Agent": UA, Accept: "application/json" }, signal: AbortSignal.timeout(8000) }
    );
    const notFound = () => json({ success: false, error: "Lyrics not found", source: "Lyrics.ovh" }, 404);
    if (up.status === 404) return notFound();
    if (!up.ok) throw new Error(`HTTP ${up.status}`);
    const payload = await up.json();
    const text = payload && typeof payload.lyrics === "string" ? payload.lyrics : "";
    if (!text.trim()) return notFound();
    const result = { success: true, lyrics: text.trim(), source: "Lyrics.ovh" };
    setCache(lyricsCache, key, result);
    return json(result);
  } catch {
    return json({ success: false, error: "Lyrics provider temporarily unavailable", source: "Lyrics.ovh" }, 502);
  }
}

async function legacyLyrics(url) {
  const track = (url.searchParams.get("track") || "").trim();
  const artist = (url.searchParams.get("artist") || "").trim();
  if (!track) return json({ success: false, error: "track required" }, 400);
  const key = `${track}_${artist}`.toLowerCase();
  const cached = getCache(lyricsCache, key);
  if (cached !== null) return json(cached);
  const p = new URLSearchParams({ track_name: track });
  if (artist) p.set("artist_name", artist);
  try {
    const up = await fetch(`https://lrclib.net/api/get?${p}`, {
      headers: { "User-Agent": UA },
      signal: AbortSignal.timeout(10000),
    });
    if (up.status === 404) return json({ success: false, error: "Lyrics not found" }, 404);
    const payload = await up.json();
    setCache(lyricsCache, key, payload);
    return json(payload, up.status);
  } catch (e) {
    console.warn("Legacy lyrics request failed:", String(e));
    return json({ success: false, error: "Lyrics provider temporarily unavailable" }, 502);
  }
}

// ---- legacy routes ----
async function legacySearch(url) {
  const query = (url.searchParams.get("q") || "").trim();
  if (!query) return json({ error: "query required" }, 400);
  const key = `global:${query.toLowerCase()}`;
  const cached = getCache(searchCache, key);
  if (cached !== null) return json(cached);
  try {
    const payload = await upstreamJson(`${BASE_API}/search`, { query });
    setCache(searchCache, key, payload);
    return json(payload);
  } catch (e) {
    console.warn("Legacy global search failed:", String(e));
    return json({ error: "Search temporarily unavailable" }, 502);
  }
}

async function suggest(url) {
  const query = (url.searchParams.get("q") || "").trim();
  if (!query) return json([]);
  const out = [];
  const seen = new Set();
  for (const song of (await searchSongs(query)).slice(0, 20)) {
    const title = String(song.title || song.name || "").trim();
    const artist = songArtist(song);
    const fp = `${normalText(title)}\u0000${normalText(artist)}`;
    if (title && !seen.has(fp)) {
      seen.add(fp);
      out.push({ id: song.id, title, artist, image: bestImage(song.image) });
    }
    if (out.length >= 10) break;
  }
  return json(out);
}

async function artistSongs(name) {
  const artistName = (name || "").trim();
  if (!artistName) return json({ artist: "", songs: [] });
  const target = normalText(artistName);
  const matched = (await searchSongs(artistName)).filter(
    (s) => target && normalText(songArtist(s)).includes(target)
  );
  return json({ artist: artistName, songs: matched.slice(0, 50) });
}

async function trending() {
  const cached = getCache(trendingCache, "global_trending");
  if (cached !== null) return json(cached);
  const queries = ["The Weeknd", "Arijit Singh", "Ed Sheeran", "Pritam", "Imagine Dragons"];
  const lists = await Promise.all(queries.map((q) => searchSongs(q)));
  const results = [];
  const seen = new Set();
  for (const list of lists) {
    for (const song of list.slice(0, 5)) {
      const sid = String(song.id || "");
      if (sid && !seen.has(sid)) {
        seen.add(sid);
        results.push(song);
      }
    }
  }
  const payload = { songs: results.slice(0, 20) };
  setCache(trendingCache, "global_trending", payload);
  return json(payload);
}

async function bulkSongs(url) {
  const ids = (url.searchParams.get("ids") || "")
    .split(",").map((s) => s.trim()).filter(Boolean).slice(0, MAX_BULK_IDS);
  if (!ids.length) return json([]);
  let songs = [];
  try {
    const payload = await upstreamJson(`${BASE_API}/songs`, { ids: ids.join(",") });
    if (payload && Array.isArray(payload.data)) songs = payload.data;
  } catch {
    songs = [];
  }
  if (!songs.length) {
    // Workers free plan allows 50 subrequests per request, so cap the fallback fan-out.
    const found = await Promise.all(ids.slice(0, 45).map(async (sid) => firstSong(await fetchSingleSongRaw(sid))));
    songs = found.filter(Boolean);
  }
  return json(songs.map((s) => ({ id: s.id, title: songTitle(s), artist: songArtist(s), image: bestImage(s.image) })));
}

async function bulkRecommendations(url) {
  const ids = (url.searchParams.get("ids") || "")
    .split(",").map((s) => s.trim()).filter(Boolean).slice(0, 3);
  const all = [];
  const seenIds = new Set();
  const seenFp = new Set();
  for (const sid of ids) {
    for (const song of await generateRecommendations(sid)) {
      const id = String(song.id || "");
      const fp = `${normalText(songTitle(song))}\u0000${normalText(songArtist(song))}`;
      if (!id || seenIds.has(id) || seenFp.has(fp)) continue;
      seenIds.add(id);
      seenFp.add(fp);
      all.push(song);
      if (all.length >= 50) return json({ recommendations: all });
    }
  }
  return json({ recommendations: all });
}

// ---- router ----
async function route(request, url) {
  const p = url.pathname;
  const seg = (prefix) => {
    const rest = p.slice(prefix.length);
    try { return decodeURIComponent(rest); } catch { return rest; }
  };

  if (p === "/") {
    return json({ status: "online", name: "Royverse Music API gana sun le lore api kya check akr rha 😁· Mujic backend", version: "2.0.0" });
  }
  if (p === "/health") return json({ status: "ok", timestamp: Math.floor(Date.now() / 1000) });
  if (p === "/stats") {
    return json({
      search_cache: searchCache.size, song_cache: songCache.size, lyrics_cache: lyricsCache.size,
      recommend_cache: recommendCache.size, trending_cache: trendingCache.size,
    });
  }
  if (p.startsWith("/api/")) return catalogueProxy(url, request);
  if (p === "/media") return mediaProxy(url, request);
  if (p === "/_diag") return diag(request);

  if (p === "/lyrics/search") {
    if (!(url.searchParams.get("q") || "").trim()) return json({ error: "query required" }, 400);
    return fixedLyricsRequest("https://lrclib.net/api/search", url.search);
  }
  if (p === "/lyrics/get") {
    if (!(url.searchParams.get("track_name") || "").trim()) return json({ error: "track_name required" }, 400);
    return fixedLyricsRequest("https://lrclib.net/api/get", url.search);
  }
  if (p === "/lyrics/ovh") return lyricsOvh(url);
  if (p === "/lyricsplus/v2/lyrics/get") return lyricsPlus(url);
  if (p === "/lyrics") return legacyLyrics(url);

  if (p === "/search") return legacySearch(url);
  if (p === "/suggest") return suggest(url);
  if (p.startsWith("/artist/")) return artistSongs(seg("/artist/"));
  if (p === "/trending") return trending();
  if (p === "/songs/bulk") return bulkSongs(url);
  if (p === "/recommendations/bulk") return bulkRecommendations(url);

  if (p.startsWith("/song/") && !seg("/song/").includes("/")) {
    const payload = await fetchSingleSongRaw(seg("/song/"));
    if (payload && firstSong(payload)) return json(payload);
    return json({ error: "Song not found" }, 404);
  }
  if (p.startsWith("/stream/") && !seg("/stream/").includes("/")) {
    const id = seg("/stream/");
    const stream = await extractStreamUrl(id);
    if (!stream) return json({ error: "Stream unavailable" }, 404);
    const current = firstSong(await fetchSingleSongRaw(id)) || {};
    return json({ id, title: songTitle(current) || "Unknown", stream });
  }
  if (p.startsWith("/queue/") && !seg("/queue/").includes("/")) {
    const id = seg("/queue/");
    const current = firstSong(await fetchSingleSongRaw(id));
    if (!current) return json({ error: "Song not found" }, 404);
    return json({ current, queue: await generateRecommendations(id) });
  }
  if (p.startsWith("/preload/") && !seg("/preload/").includes("/")) {
    const id = seg("/preload/");
    const currentStream = await extractStreamUrl(id);
    const queue = await generateRecommendations(id);
    const next = queue.length ? queue[0] : null;
    const nextStream = next && next.id ? await extractStreamUrl(String(next.id)) : null;
    return json({ stream: currentStream, next_stream: nextStream, queue: queue.slice(1) });
  }

  return json({ error: "Not found" }, 404);
}

// ---- origin allowlist (CORS) ----
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
function originAllowed(origin) {
  if (ALLOWED_ORIGINS.includes("*")) return true;
  return ALLOWED_ORIGINS.includes(origin) || LOCAL_ORIGIN.test(origin);
}

// ---- edge cache: /api/* JSON and /media artwork only (never audio, never Range requests) ----
async function cachedRoute(request, url, ctx) {
  const p = url.pathname;
  const isApi = p.startsWith("/api/");
  const isMedia = p === "/media" && !request.headers.get("Range");
  if (request.method !== "GET" || !(isApi || isMedia)) return route(request, url);

  const cache = caches.default;
  const key = new Request(url.toString(), { method: "GET" });
  const hit = await cache.match(key);
  if (hit) {
    const r = new Response(hit.body, hit);
    r.headers.set("X-Cache", "HIT");
    return r;
  }

  const res = await route(request, url);
  if (res.headers.has("X-No-Edge-Cache")) {
    const o = new Response(res.body, res);
    o.headers.delete("X-No-Edge-Cache");
    return o;
  }
  if (res.status !== 200) return res;
  const type = res.headers.get("Content-Type") || "";
  const storeIt = (isApi && type.includes("json")) || (isMedia && type.startsWith("image/"));
  if (!storeIt) return res;

  const out = new Response(res.body, res);
  out.headers.set(
    "Cache-Control",
    isApi
      ? `public, max-age=300, s-maxage=${p.startsWith("/api/search") ? 300 : CACHE_TTL}`
      : "public, max-age=900, s-maxage=604800"
  );
  out.headers.set("X-Cache", "MISS");
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

// ---- CORS + cache headers (mirrors Flask after_request, with an origin allowlist) ----
function finalize(request, url, response) {
  const res = new Response(response.body, response);
  const h = res.headers;
  const origin = request.headers.get("Origin");
  if (ALLOWED_ORIGINS.includes("*")) h.set("Access-Control-Allow-Origin", "*");
  else if (origin && originAllowed(origin)) h.set("Access-Control-Allow-Origin", origin);
  h.append("Vary", "Origin");
  h.set("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, ETag");
  if (request.method === "GET" || request.method === "HEAD") {
    if (res.status === 200 && url.pathname !== "/health" && url.pathname !== "/stats") {
      if (!h.has("Cache-Control")) h.set("Cache-Control", "public, max-age=300");
    }
  }
  if (res.status >= 400) h.set("Cache-Control", "no-store");
  if (!h.has("X-Content-Type-Options")) h.set("X-Content-Type-Options", "nosniff");
  return res;
}

export default {
  async fetch(request, env, ctx) {
    BASE_API = (env.UPSTREAM_CATALOGUE_API || BASE_API).replace(/\/+$/, "");
    CACHE_TTL = parseInt(env.CACHE_TTL_SECONDS || "1800", 10);
    TIMEOUT_MS = parseFloat(env.UPSTREAM_TIMEOUT_SECONDS || "18") * 1000;
    ALLOWED_ORIGINS = (env.ALLOWED_ORIGINS || "https://royverse69.github.io")
      .split(",").map((s) => s.trim()).filter(Boolean);
    CATALOGUE_FALLBACK = (env.CATALOGUE_FALLBACK_API || "").replace(/\/+$/, "");
    MEDIA_FALLBACK = (env.MEDIA_FALLBACK_ORIGIN || "").replace(/\/+$/, "");

    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    // Browsers from any other website are refused (requests without an Origin header, e.g. <audio>, curl, still work).
    if (origin && !originAllowed(origin)) {
      return finalize(request, url, json({ error: "Origin not allowed" }, 403));
    }
    if (request.method === "OPTIONS") {
      return finalize(request, url, new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
          "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "*",
          "Access-Control-Max-Age": "86400",
        },
      }));
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return finalize(request, url, json({ error: "Method not allowed" }, 405));
    }
    let response;
    try {
      response = await cachedRoute(request, url, ctx);
    } catch (e) {
      console.error("Unhandled error:", String(e));
      response = json({ error: "Internal server error" }, 500);
    }
    return finalize(request, url, response);
  },
};
