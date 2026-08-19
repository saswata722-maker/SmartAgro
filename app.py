from flask import Flask, render_template, request, jsonify, g
import requests
import logging
import uuid

# ── Windows fix: force IPv4 for outbound requests ───────────────────────────
# If a browser reaches a URL instantly but Python's `requests` times out on
# the exact same URL, it is almost always because requests/urllib3 tries
# IPv6 first and your network's IPv6 path is broken or very slow, while the
# browser silently falls back to IPv4 in milliseconds. This forces Python's
# HTTP stack to only use IPv4, matching what the browser effectively does.
try:
    import socket
    import urllib3.util.connection as urllib3_conn

    def _allowed_gai_family():
        return socket.AF_INET  # IPv4 only

    urllib3_conn.allowed_gai_family = _allowed_gai_family
except Exception as _e:
    print(f"[AgroSmart] Could not force IPv4 (non-fatal): {_e}")
import os
import json
import re
import time
import base64
import hashlib
import difflib
import threading
import gzip
import concurrent.futures
from datetime import datetime, timedelta
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# ── Structured logging ───────────────────────────────────────────────────────
# All logs go through Python's `logging` (instead of bare print) so they carry a
# timestamp, a severity level, and a per-request ID for tracing. Set LOG_LEVEL in
# .env to change verbosity (INFO default). API keys are never logged.
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smartagro")


@app.before_request
def _request_start():
    g._start = time.monotonic()
    g.request_id = uuid.uuid4().hex[:10]


@app.after_request
def _log_request(response):
    dur_ms = (time.monotonic() - g.get("_start", time.monotonic())) * 1000
    logger.info("%s %s -> %s (%.0fms) rid=%s",
                request.method, request.path, response.status_code, dur_ms, g.get("request_id", "-"))
    return response


@app.after_request
def _maybe_gzip(response):
    """gzip-compress text responses when the client accepts it (cuts HTML/CSS/
    JS/JSON transfer sizes ~70%). Skips errors, tiny bodies, non-text types,
    and anything that is already compressed."""
    if request.method == "HEAD" or response.status_code >= 400:
        return response
    if getattr(response, "direct_passthrough", False):
        return response
    if response.headers.get("Content-Encoding"):
        return response
    ct = response.headers.get("Content-Type", "").split(";")[0]
    if not any(ct.startswith(p) for p in ("text/", "application/javascript",
            "application/json", "application/xml", "image/svg+xml")):
        return response
    payload = response.get_data()
    if len(payload) < 512 or "gzip" not in request.headers.get("Accept-Encoding", ""):
        return response
    try:
        compressed = gzip.compress(payload, compresslevel=6)
    except Exception:
        return response
    if len(compressed) >= len(payload):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.headers.pop("ETag", None)
    try:
        response.vary.add("Accept-Encoding")
    except Exception:
        pass
    return response


# ── Cache-busting: every static asset URL gets ?v=<file-mtime> so updates are
# seen immediately even when a browser/PWA cache is sticky.
def _static_version(filename):
    try:
        return int(os.path.getmtime(os.path.join(app.static_folder or "static", filename)))
    except Exception:
        return None


@app.context_processor
def _inject_static_url():
    def static_url(filename):
        v = _static_version(filename)
        return "/static/" + filename + (f"?v={v}" if v else "")
    return {"static_url": static_url}


from flask import url_for as _flask_url_for

def _hashed_url_for(endpoint, **values):
    if endpoint == "static" and values.get("filename"):
        v = _static_version(str(values["filename"]))
        if v:
            values["v"] = v
    return _flask_url_for(endpoint, **values)

app.jinja_env.globals["url_for"] = _hashed_url_for

# ── Per-feature usage analytics ─────────────────────────────────────────────
# Tracks how often each SmartAgro feature is used (page views + API calls) as
# aggregate counters — NO personal data, NO IPs, NO message content. Counters
# live in memory and are persisted atomically to a JSON file in batches, so the
# app never does disk I/O on every request.
USAGE_LOG_PATH = os.path.join(basedir, "usage_stats.json")
_USAGE_SAVE_INTERVAL = 30  # seconds between automatic disk writes

_usage_stats = None           # {"since": iso, "features": {ep: {...}}, "daily": {date: count}}
_usage_lock = threading.Lock()
_last_usage_save = 0.0

# endpoint (view-function name) → (friendly label, kind)
_USAGE_FEATURE_LABELS = {
    "index":                       ("Dashboard Page",               "page"),
    "diagnose":                    ("Crop Diagnosis Page",         "page"),
    "market":                      ("Market Page",                 "page"),
    "alerts":                      ("Alerts Page",                 "page"),
    "offline":                     ("Offline Page",                "page"),
    "get_ndvi":                    ("Satellite NDVI",              "api"),
    "get_weather":                 ("Live Weather",                "api"),
    "crop_recommendations":        ("Crop Recommendations",        "api"),
    "get_market_data":             ("Mandi Market Prices",         "api"),
    "debug_market":                ("Market Debug",                "api"),
    "kisan_chat":                  ("Kisan Helper Chat",           "api"),
    "speech_to_text":              ("Voice Input (STT)",           "api"),
    "diagnose_crop":               ("Crop Diagnosis",              "api"),
    "diagnose_log":                ("Diagnosis QA Log",            "api"),
    "diagnose_log_image":          ("Diagnosis QA Image",          "api"),
    "diagnose_log_review":         ("Diagnosis Review",            "api"),
    "diagnose_log_accuracy":       ("Diagnosis Accuracy",          "api"),
    "get_alerts":                  ("Instant Alerts",              "api"),
    "alerts_forecast":             ("6-Day Forecast Alerts",       "api"),
    "monthly_alerts":              ("Monthly Outlook Alerts",      "api"),
    "seasonal_alerts":             ("Seasonal Advisories",         "api"),
    "crop_risk":                   ("Crop Risk / Harvest Window",  "api"),
    "translate_market":            ("Market Translation",          "api"),
    "clear_translation_cache":     ("Translation Cache Clear",     "api"),
    "translate_alerts":            ("Alerts Translation",          "api"),
    "translate_dashboard":         ("Dashboard Translation",       "api"),
    "translate_diagnose":          ("Diagnose Translation",        "api"),
    "translate_diagnosis_result":  ("Diagnosis Result Translation","api"),
}


def _load_usage_stats():
    global _usage_stats
    if _usage_stats is not None:
        return
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            _usage_stats = json.load(f)
    except Exception:
        _usage_stats = {}
    _usage_stats.setdefault("since", datetime.now().isoformat(timespec="seconds"))
    _usage_stats.setdefault("features", {})
    _usage_stats.setdefault("daily", {})


def _save_usage_stats():
    """Atomic write (tmp file + rename) so a crash can never corrupt the log."""
    if _usage_stats is None:
        return
    try:
        tmp_path = USAGE_LOG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_usage_stats, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, USAGE_LOG_PATH)
    except Exception as e:
        logger.warning(f"[Usage] Could not persist usage stats: {e}")


def _flush_usage_stats():
    """Force a disk write so a reader always sees the freshest counters."""
    global _last_usage_save
    with _usage_lock:
        _load_usage_stats()
        _save_usage_stats()
        _last_usage_save = time.monotonic()


def _track_usage(endpoint, label=None, kind="api"):
    global _last_usage_save
    now = datetime.now()
    with _usage_lock:
        _load_usage_stats()
        feats = _usage_stats["features"]
        rec = feats.setdefault(endpoint, {
            "label": label or endpoint, "kind": kind, "count": 0,
        })
        rec["count"] += 1
        rec["last_used"] = now.isoformat(timespec="seconds")
        today = now.strftime("%Y-%m-%d")
        _usage_stats["daily"][today] = _usage_stats["daily"].get(today, 0) + 1
        if time.monotonic() - _last_usage_save >= _USAGE_SAVE_INTERVAL:
            _last_usage_save = time.monotonic()
            _save_usage_stats()


@app.before_request
def _track_usage_request():
    # Count every feature hit automatically. Health probes, static assets and
    # the analytics endpoints themselves are excluded so they don't skew stats.
    ep = request.endpoint or ""
    if not ep or ep == "static" or ep in ("usage", "usage_api", "usage_reset", "healthz"):
        return
    label, kind = _USAGE_FEATURE_LABELS.get(ep, (None, "api"))
    _track_usage(ep, label if label is not None else ep, kind)


# ── API keys / secrets — ALWAYS from environment (.env), never hardcoded ────
# Every value below comes exclusively from os.getenv(). None of them has a
# real credential baked in as a fallback default: if a variable is missing
# from .env, the app runs with that feature degraded/disabled and prints a
# warning at startup, rather than silently falling back to an embedded key.
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
DATA_GOV_API_KEY    = os.getenv("DATA_GOV_API_KEY", "")
DEBUG_MODE          = os.getenv("FLASK_DEBUG", "0") == "1"

# Gemini is used as a genuinely INDEPENDENT second vision model in the crop
# diagnosis ensemble. Only active when GEMINI_API_KEY is set in .env — without
# it the ensemble simply runs the Groq vision passes as before.
GEMINI_DIAGNOSIS_MODEL = os.getenv("GEMINI_DIAGNOSIS_MODEL", "gemini-3.1-flash-lite")

# ── AI text-provider: Gemini (free tier) for ALL text helpers ───────────────
# Kisan Helper chat, the topic gate, CropAI recommendations and multi-language
# translation all use Google's Gemini flash-lite via its OpenAI-compatible
# endpoint (free tier). Groq now remains ONLY for Whisper speech-to-text
# (/api/stt) and the crop-diagnosis vision ensemble, which both require it.
AI_COMPLETIONS_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
AI_CHAT_MODEL      = os.getenv("AGRO_AI_CHAT_MODEL", "gemini-3.5-flash-lite")

def _ai_headers():
    """Authorization headers for the Gemini OpenAI-compatible endpoint."""
    return {"Authorization": f"Bearer {GEMINI_API_KEY}", "Content-Type": "application/json"}

# ── Optional heavy deps: Sentinel-2 NDVI (live satellite vegetation) ─────
# rasterio + numpy are only needed for the REAL Sentinel-2 vegetation-health
# feature. On systems where they aren't installed the app still runs fully —
# the /api/ndvi endpoint transparently falls back to a clearly-labelled
# seasonal estimate instead of breaking.
try:
    import numpy as np                       # noqa: F401  (kept for rasterio COG math)
    import rasterio
    from rasterio.transform import rowcol
    from rasterio.warp import transform as _rasterio_warp
    _RASTERIO_AVAILABLE = True
except ImportError:
    _RASTERIO_AVAILABLE = False
    logger.warning("[AgroSmart] rasterio/numpy not installed — NDVI will use estimation fallback")

# ── Diagnosis QA logging ─────────────────────────────────────────────────
# Every diagnosis (image + full model output, from every ensemble pass) is
# persisted here so a human can spot-check the AI against the real photo
# later. This is the "proof of accuracy" pipeline: without stored
# input/output pairs there is nothing to audit or compute real accuracy
# metrics from, no matter what confidence number the model reports.
DIAGNOSIS_LOG_DIR    = os.getenv("DIAGNOSIS_LOG_DIR", os.path.join(os.path.expanduser("~"), "SmartAgro_Logs"))
DIAGNOSIS_IMAGES_DIR = os.path.join(DIAGNOSIS_LOG_DIR, "images")
DIAGNOSIS_LOG_PATH   = os.path.join(DIAGNOSIS_LOG_DIR, "log.jsonl")
os.makedirs(DIAGNOSIS_IMAGES_DIR, exist_ok=True)
_diagnosis_log_lock = threading.Lock()
if os.getenv("SPACE_ID"):
    logger.warning(
        "On Hugging Face Spaces the diagnosis QA logs/images (%s) are EPHEMERAL "
        "and are wiped on every redeploy. Set DIAGNOSIS_LOG_DIR to a mounted "
        "volume if the accuracy audit trail matters.", DIAGNOSIS_LOG_DIR,
    )

# Number of independent diagnosis passes to run and cross-check per image.
# If `vision_models` (defined near the /api/diagnose route below) only has
# one production-viable entry, this runs that same model twice at different
# temperatures as a self-consistency check. The moment Groq exposes a
# second distinct vision model on the general tier, just add it to
# `vision_models` and these passes automatically become a true multi-model
# ensemble with zero other code changes.
ENSEMBLE_PASSES = 2

_translation_cache = {}

LANG_NAMES = {
    "en":"English","hi":"Hindi","bn":"Bengali","te":"Telugu","mr":"Marathi",
    "ta":"Tamil","gu":"Gujarati","kn":"Kannada","ml":"Malayalam","pa":"Punjabi",
    "or":"Odia","as":"Assamese","ur":"Urdu","mai":"Maithili","sat":"Santali",
    "ks":"Kashmiri","ne":"Nepali","sd":"Sindhi","kok":"Konkani","mni":"Manipuri",
    "bodo":"Bodo","doi":"Dogri","sa":"Sanskrit",
}

# ── Startup diagnostics — report key status WITHOUT ever printing the
# actual key values. Missing DATA_GOV_API_KEY is non-fatal (data.gov.in
# allows limited public access), but Groq/Weather being missing means
# those features simply won't work until .env is filled in.
logger.info(f"[AgroSmart] Groq key:      {'OK' if GROQ_API_KEY else 'MISSING — set GROQ_API_KEY in .env'}")
logger.info(f"[AgroSmart] Weather key:   {'OK' if OPENWEATHER_API_KEY else 'MISSING — set OPENWEATHER_API_KEY in .env'}")
logger.info(f"[AgroSmart] Gemini key:   {'OK' if GEMINI_API_KEY else 'unset — diagnosis uses Groq only'}")
logger.info(f"[AgroSmart] NDVI:         {'ENABLED (live Sentinel-2 via rasterio)' if _RASTERIO_AVAILABLE else 'ESTIMATE-ONLY (install rasterio+numpy for live Sentinel-2 NDVI)'}")
logger.info(f"[AgroSmart] data.gov.in key: {'OK' if DATA_GOV_API_KEY else 'MISSING — set DATA_GOV_API_KEY in .env (get a free key at https://data.gov.in). Market data will use the offline fallback until then.'}")

# ─── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/diagnose")
def diagnose():
    return render_template("diagnose.html")

@app.route("/market")
def market():
    return render_template("market.html")

@app.route("/alerts")
def alerts():
    return render_template("alerts.html")

@app.route('/offline')
def offline():
    return render_template('offline.html')


# ─── Usage Analytics (per-feature) ───────────────────────────────────────────
@app.route("/usage")
def usage():
    return render_template("usage.html")


@app.route("/api/usage")
def usage_api():
    _flush_usage_stats()
    with _usage_lock:
        stats = json.loads(json.dumps(_usage_stats))
    features = stats.get("features", {})
    rows = sorted(features.items(), key=lambda kv: -kv[1]["count"])
    total = sum(rec["count"] for _, rec in rows)
    by_kind = {}
    for _, rec in rows:
        by_kind[rec["kind"]] = by_kind.get(rec["kind"], 0) + rec["count"]
    return jsonify({
        "since":   stats.get("since"),
        "total":   total,
        "by_kind": by_kind,
        "daily":   stats.get("daily", {}),
        "features": [
            {"endpoint": ep, "label": rec["label"], "kind": rec["kind"],
             "count": rec["count"], "last_used": rec.get("last_used")}
            for ep, rec in rows
        ],
    })


@app.route("/api/usage/reset", methods=["POST"])
def usage_reset():
    global _usage_stats
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    with _usage_lock:
        _usage_stats = {"since": datetime.now().isoformat(timespec="seconds"),
                        "features": {}, "daily": {}}
        _save_usage_stats()
    return jsonify({"ok": True, "total": 0})


@app.route('/healthz')
def healthz():
    """Liveness probe for load balancers / uptime checks. No secrets."""
    return jsonify({"status": "ok"})


@app.route('/readyz')
def readyz():
    """Readiness probe: reports whether each optional feature is configured,
    WITHOUT exposing any actual key value."""
    return jsonify({
        "status": "ok",
        "groq_configured":      bool(GROQ_API_KEY),
        "weather_configured":   bool(OPENWEATHER_API_KEY),
        "gemini_configured":    bool(GEMINI_API_KEY),
        "market_configured":    bool(DATA_GOV_API_KEY),
        "ndvi_live":            _RASTERIO_AVAILABLE,
    })

# ─── Satellite Vegetation Health — Sentinel-2 NDVI ───────────────────────────
# Real NDVI via free Element84 "Earth Search" STAC catalog + direct COG pixel
# reads from Sentinel-2 L2A imagery (ESA/Copernicus). When rasterio isn't
# installed, or no cloud-free recent scene covers the point, the endpoint
# falls back to a clearly-labelled seasonal estimate so the UI never breaks.
_ndvi_cache = {}
_NDVI_CACHE_TTL = 6 * 3600   # seconds — a fresh Sentinel-2 scene only arrives ~daily
_ndvi_lock = threading.Lock()


def _ndvi_status(ndvi):
    """Map an NDVI value to a plain-language vegetation status label."""
    if ndvi is None:
        return "Unavailable"
    if ndvi < 0.0:
        return "Water / Cloud / No Data"
    if ndvi < 0.1:
        return "Bare Soil / Urban / Rock"
    if ndvi < 0.2:
        return "Sparse Vegetation / Bare Soil"
    if ndvi < 0.4:
        return "Moderate Vegetation"
    if ndvi < 0.6:
        return "Good Vegetation Cover"
    return "Dense / Healthy Vegetation"


def estimate_ndvi(lat, lon):
    """Deterministic seasonal-typical NDVI estimate. Used ONLY when live
    Sentinel-2 data cannot be fetched (rasterio missing, STAC unreachable,
    no recent cloud-free scene). Always clearly tagged source='estimate' —
    it is a farming-season heuristic, never presented as live satellite data."""
    month = datetime.now().month
    if month in (6, 7, 8, 9):          # Kharif monsoon — peak biomass
        seasonal_base = 0.82
    elif month in (10, 11, 12, 1, 2):  # Rabi winter — good cover
        seasonal_base = 0.70
    else:                              # Zaid summer — lower cover
        seasonal_base = 0.55
    seed = int(hashlib.md5(f"{round(lat,2)}|{round(lon,2)}|{month}".encode('utf-8')).hexdigest(), 16)
    ndvi = max(0.10, min(0.90, seasonal_base + ((seed % 100) / 100.0 - 0.5) * 0.12))
    return {
        "ndvi":      round(ndvi, 3),
        "status":    _ndvi_status(ndvi),
        "obs_date":  None,
        "cloud_pct": None,
        "source":    "estimate",
        "lat":       round(lat, 4),
        "lon":       round(lon, 4),
    }


def get_sentinel2_ndvi(lat, lon):
    """Fetch a REAL NDVI value at (lat, lon) from Sentinel-2 L2A imagery.
    Returns a dict {ndvi, status, obs_date, cloud_pct, source} or None when
    the real-data pipeline can't resolve a value (caller falls back)."""
    if not _RASTERIO_AVAILABLE:
        return None
    cache_key = f"{round(lat, 2)},{round(lon, 2)}"  # ~1 km grid cell
    now = time.time()
    with _ndvi_lock:
        cached = _ndvi_cache.get(cache_key)
        if cached and (now - cached["ts"]) < _NDVI_CACHE_TTL:
            return cached["data"]

    try:
        # 1) Find the most recent cloud-free-enough Sentinel-2 L2A scene.
        stac_resp = requests.post(
            "https://earth-search.aws.element84.com/v1/search",
            json={
                "collections": ["sentinel-2-l2a"],
                "query": {"eo:cloud_cover": {"lt": 40}},
                "intersects": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "sortby": [{"field": "properties.datetime", "direction": "desc"}],
                "limit": 3,
            },
            timeout=20,
        )
        if stac_resp.status_code != 200:
            logger.warning(f"[NDVI] STAC HTTP {stac_resp.status_code} — using estimate")
            return None
        features = (stac_resp.json().get("features") or [])
        if not features:
            logger.warning("[NDVI] No Sentinel-2 scenes found for point — using estimate")
            return None
        feat      = features[0]
        props     = feat.get("properties") or {}
        obs_date  = (props.get("datetime") or "")[:10]
        cloud_pct = props.get("eo:cloud_cover")
        epsg      = props.get("proj:epsg")
        assets    = feat.get("assets") or {}
        red_asset = assets.get("red") or assets.get("B04")
        nir_asset = assets.get("nir") or assets.get("B08")
        if not (epsg and red_asset and nir_asset):
            return None

        # 2) Transform the lon/lat point into the scene's CRS and read the
        #    1-pixel window from each COG (NIR & Red) directly over HTTPS.
        src_x, src_y = _rasterio_warp("EPSG:4326", f"EPSG:{epsg}", [float(lon)], [float(lat)])
        red_val = nir_val = None
        with rasterio.Env():
            with rasterio.open(red_asset["href"]) as ds:
                row, col = rowcol(ds.transform, src_x[0], src_y[0])
                if 0 <= row < ds.height and 0 <= col < ds.width:
                    red_val = float(ds.read(1, window=((row, row + 1), (col, col + 1)))[0][0])
            with rasterio.open(nir_asset["href"]) as ds2:
                row, col = rowcol(ds2.transform, src_x[0], src_y[0])
                if 0 <= row < ds2.height and 0 <= col < ds2.width:
                    nir_val = float(ds2.read(1, window=((row, row + 1), (col, col + 1)))[0][0])
        if red_val is None or nir_val is None or (nir_val + red_val) == 0:
            logger.warning(f"[NDVI] Sensor value issue (red={red_val}, nir={nir_val}) — using estimate")
            return None

        ndvi = round(max(-1.0, min(1.0, (nir_val - red_val) / (nir_val + red_val))), 3)
        result = {
            "ndvi":      ndvi,
            "status":    _ndvi_status(ndvi),
            "obs_date":  obs_date,
            "cloud_pct": cloud_pct,
            "source":    "Sentinel-2 L2A",
            "lat":       round(lat, 4),
            "lon":       round(lon, 4),
        }
        with _ndvi_lock:
            _ndvi_cache[cache_key] = {"ts": now, "data": result}
        logger.info(f"[NDVI] Sentinel-2 OK: NDVI={ndvi}, status='{result['status']}'")
        return result
    except Exception as e:
        logger.warning(f"[NDVI] error: {e} — using estimate")
        return None


@app.route("/api/ndvi")
def get_ndvi():
    """Vegetation health for a location. Returns LIVE Sentinel-2 NDVI when
    the real-data pipeline succeeds, with a clearly-tagged seasonal estimate
    as fallback. The `source` field tells the UI which one the farmer sees."""
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    result = get_sentinel2_ndvi(lat, lon)
    if result is None:
        result = estimate_ndvi(lat, lon)
    return jsonify(result)


# Small TTL cache so repeated chat weather/alerts asks (and dashboard reloads)
# never hammer OpenWeather for the same spot within a few minutes.
_weather_cache = {}
_WEATHER_CACHE_TTL = 300  # seconds

def _fetch_current_weather(lat, lon):
    """Live OpenWeather current + 7-day forecast. Shared by the /api/weather
    route AND the Kisan Helper feature gateway, so the chatbot's answers are
    built from the exact same data the dashboard shows. Returns the same dict
    shape as /api/weather, or None on any failure / missing API key."""
    if not OPENWEATHER_API_KEY:
        return None

    cache_key = f"{round(lat, 2)},{round(lon, 2)}"
    now_ts = time.monotonic()
    cached = _weather_cache.get(cache_key)
    if cached and (now_ts - cached[0]) < _WEATHER_CACHE_TTL:
        return cached[1]

    try:
        current_resp  = requests.get("https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"}, timeout=10)
        forecast_resp = requests.get("https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "cnt": 56}, timeout=10)

        if current_resp.status_code != 200:
            logger.warning(f"[Weather] Current API error: {current_resp.text}")
            return None
        if forecast_resp.status_code != 200:
            logger.warning(f"[Weather] Forecast API error: {forecast_resp.text}")

        current_data  = current_resp.json()
        forecast_data = forecast_resp.json()

        daily = {}
        if forecast_data.get("list"):
            for item in forecast_data["list"]:
                day = datetime.fromtimestamp(item["dt"]).strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {
                        "date":        day,
                        "temp_max":    item["main"]["temp_max"],
                        "temp_min":    item["main"]["temp_min"],
                        "description": item["weather"][0]["description"],
                        "icon":        item["weather"][0]["icon"],
                        "humidity":    item["main"]["humidity"],
                        "wind_speed":  item["wind"]["speed"],
                        "rain":        item.get("rain", {}).get("3h", 0),
                    }
                else:
                    if item["main"]["temp_max"] > daily[day]["temp_max"]:
                        daily[day]["temp_max"] = item["main"]["temp_max"]
                    if item["main"]["temp_min"] < daily[day]["temp_min"]:
                        daily[day]["temp_min"] = item["main"]["temp_min"]

        forecast_list = list(daily.values())[:7]

        result = {
            "current": {
                "city":        current_data.get("name", "Your Location"),
                "lat":         float(lat),
                "lon":         float(lon),
                "temp":        round(current_data["main"]["temp"]),
                "feels_like":  round(current_data["main"]["feels_like"]),
                "humidity":    current_data["main"]["humidity"],
                "description": current_data["weather"][0]["description"],
                "icon":        current_data["weather"][0]["icon"],
                "wind_speed":  current_data.get("wind", {}).get("speed", 0),
                "pressure":    current_data["main"].get("pressure", 0),
                "visibility":  current_data.get("visibility", 0) / 1000,
                "rain":        current_data.get("rain", {}).get("1h", 0),
            },
            "forecast": forecast_list
        }
        _weather_cache[cache_key] = (time.monotonic(), result)
        return result
    except Exception as e:
        logger.warning(f"[Weather error] {e}")
        return None


# ─── Weather API ─────────────────────────────────────────────────────────────
# ─── Weather API ─────────────────────────────────────────────────────────────
@app.route("/api/weather")
def get_weather():
    if not OPENWEATHER_API_KEY:
        return jsonify({"error": "Weather is not configured on this server. Set OPENWEATHER_API_KEY in .env."}), 500

    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400

    weather = _fetch_current_weather(lat, lon)
    if weather is None:
        return jsonify({"error": "Weather service unavailable right now. Please try again in a moment."}), 500
    return jsonify(weather)


# ─── Crop Recommendations ────────────────────────────────────────────────────
_crop_ai_cache = {}
CROP_AI_CACHE_TTL_SEC = 3 * 60 * 60  # 3 hours — same city/season/weather bucket repeats a lot in a day


def ai_recommend_crops(city, lat, lon, temp, humidity, rain, season):
    """Ask Groq for crops genuinely suited to THIS location's climate, soil
    region and season — instead of matching generic temp/humidity bands
    against a fixed table.
    Returns None on any failure so the caller can fall back to the
    rule-based recommend_crops() and the dashboard never breaks."""
    if not GROQ_API_KEY:
        return None

    cache_key = f"{city}|{round((lat or 0), 1)}|{round((lon or 0), 1)}|{season}|{round(temp/3)*3}|{round(humidity/10)*10}"
    now = time.monotonic()
    cached = _crop_ai_cache.get(cache_key)
    if cached and (now - cached[0]) < CROP_AI_CACHE_TTL_SEC:
        return cached[1]

    prompt = f"""You are an agronomist advising a farmer in India.

Location: {city or "an unspecified Indian town"} (approx. lat {lat}, lon {lon})
Current season: {season}
Current weather right now: {temp} deg C, {humidity}% humidity, {rain} mm recent rainfall

Recommend the 6 crops BEST suited to THIS exact location's climate, soil
region and season — not a generic list. Use your knowledge of Indian
agro-climatic zones (e.g. black cotton soil across much of Maharashtra,
alluvial soil in the Indo-Gangetic plain, laterite soil along the Western
Ghats/coastal belts, arid/sandy soil in Rajasthan, red soil in the Deccan
plateau, etc.) to pick realistic, regionally-appropriate crops that a real
agricultural officer would suggest for this place right now, ranked by
suitability.

Respond ONLY with a JSON object, no preamble, no markdown fences, matching
exactly this shape:
{{
  "crops": [
    {{
      "name": "Crop name in English",
      "icon": "one relevant emoji",
      "match": "e.g. 92%",
      "description": "one short sentence on why it suits this location/season",
      "season": "Kharif (Monsoon) | Rabi (Winter) | Zaid (Summer)",
      "water": "Low | Medium | High | Very High",
      "yield": "e.g. 3-5 tonnes/ha",
      "profit": "e.g. Rs45,000-65,000/ha",
      "duration": "e.g. 90-150 days",
      "soil": "soil type suited to this region",
      "fertilizer": "e.g. NPK 120:60:60 kg/ha"
    }}
  ]
}}"""

    headers = _ai_headers()
    body = {
        "model":       AI_CHAT_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens":  1500,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = _post_to_ai(body, headers)
        if resp is None or resp.status_code != 200:
            logger.warning(f"[CropAI] AI HTTP {getattr(resp, 'status_code', 'no-response')} for {city}")
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        parsed = json.loads(match.group() if match else cleaned)
        crops = parsed.get("crops")
        if not isinstance(crops, list) or not crops:
            return None
        for c in crops:
            c.setdefault("icon", "🌱")
        _crop_ai_cache[cache_key] = (now, crops)
        logger.info(f"[CropAI] OK for {city}: {len(crops)} crops")
        return crops
    except Exception as e:
        logger.warning(f"[CropAI] error for {city}: {e}")
        return None


@app.route("/api/crop-recommendations", methods=["POST"])
def crop_recommendations():
    data     = request.json or {}
    temp     = data.get("temp", 25)
    humidity = data.get("humidity", 60)
    rain     = data.get("rain", 0)
    city     = data.get("city", "")
    lat      = data.get("lat")
    lon      = data.get("lon")
    season   = get_season(datetime.now().month)

    ai_crops = ai_recommend_crops(city, lat, lon, temp, humidity, rain, season)
    if ai_crops:
        crops, source = ai_crops, "ai"
    else:
        crops, source = recommend_crops(temp, humidity, rain, season), "rule_based"

    calendar = generate_advisory_calendar(crops[:3])
    return jsonify({
        "season":     season,
        "crops":      crops,
        "calendar":   calendar,
        "pesticides": get_pesticide_guide(crops[:3]),
        "source":     source,   # "ai" = location-aware, "rule_based" = offline fallback
    })


def get_season(month):
    if month in [6, 7, 8, 9]:
        return "Kharif (Monsoon)"
    elif month in [10, 11, 12, 1, 2]:
        return "Rabi (Winter)"
    else:
        return "Zaid (Summer)"


def recommend_crops(temp, humidity, rain, season):
    all_crops = [
        {"name":"Rice","icon":"🌾","temp_range":(20,38),"humidity_range":(70,100),"rain_min":15,"season":"Kharif (Monsoon)","water":"High","yield":"3-5 tonnes/ha","profit":"Rs45,000-65,000/ha","duration":"90-150 days","description":"Ideal for high humidity and warm monsoon conditions","soil":"Clay loam, alluvial","fertilizer":"NPK 120:60:60 kg/ha"},
        {"name":"Wheat","icon":"🌿","temp_range":(10,25),"humidity_range":(40,65),"rain_min":0,"season":"Rabi (Winter)","water":"Medium","yield":"4-6 tonnes/ha","profit":"Rs50,000-75,000/ha","duration":"100-150 days","description":"Best suited for cool, dry winters","soil":"Well-drained loam","fertilizer":"NPK 120:60:40 kg/ha"},
        {"name":"Maize","icon":"🌽","temp_range":(18,35),"humidity_range":(50,80),"rain_min":5,"season":"Kharif (Monsoon)","water":"Medium","yield":"5-8 tonnes/ha","profit":"Rs40,000-60,000/ha","duration":"80-110 days","description":"Versatile crop for warm humid weather","soil":"Sandy loam to clay loam","fertilizer":"NPK 150:75:75 kg/ha"},
        {"name":"Cotton","icon":"☁️","temp_range":(25,40),"humidity_range":(40,70),"rain_min":0,"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3 tonnes/ha","profit":"Rs60,000-90,000/ha","duration":"150-180 days","description":"Thrives in hot dry spells with moderate rain","soil":"Black cotton soil","fertilizer":"NPK 90:45:45 kg/ha"},
        {"name":"Tomato","icon":"🍅","temp_range":(18,30),"humidity_range":(60,80),"rain_min":0,"season":"Zaid (Summer)","water":"Medium","yield":"20-40 tonnes/ha","profit":"Rs80,000-1,50,000/ha","duration":"60-80 days","description":"High value crop for moderate climates","soil":"Sandy loam, rich organic matter","fertilizer":"NPK 100:60:60 kg/ha"},
        {"name":"Sugarcane","icon":"🎋","temp_range":(24,38),"humidity_range":(75,90),"rain_min":20,"season":"Kharif (Monsoon)","water":"Very High","yield":"70-100 tonnes/ha","profit":"Rs70,000-1,00,000/ha","duration":"300-360 days","description":"Requires hot climate and heavy rainfall/irrigation","soil":"Deep loam, good drainage","fertilizer":"NPK 250:80:100 kg/ha"},
        {"name":"Soybean","icon":"🫘","temp_range":(20,32),"humidity_range":(60,80),"rain_min":10,"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3 tonnes/ha","profit":"Rs35,000-55,000/ha","duration":"90-120 days","description":"Nitrogen-fixing legume for warm monsoon","soil":"Well-drained loam","fertilizer":"NPK 30:60:40 kg/ha"},
        {"name":"Mustard","icon":"🌻","temp_range":(10,25),"humidity_range":(40,60),"rain_min":0,"season":"Rabi (Winter)","water":"Low","yield":"1-2 tonnes/ha","profit":"Rs25,000-40,000/ha","duration":"90-110 days","description":"Cool weather oil seed crop","soil":"Sandy loam, well-drained","fertilizer":"NPK 80:40:40 kg/ha"},
        {"name":"Chickpea (Gram)","icon":"🌱","temp_range":(15,28),"humidity_range":(35,65),"rain_min":0,"season":"Rabi (Winter)","water":"Low","yield":"1.5-2.5 tonnes/ha","profit":"Rs35,000-50,000/ha","duration":"90-120 days","description":"Drought-tolerant pulse crop ideal for Rabi season","soil":"Deep sandy loam to clay loam","fertilizer":"NPK 20:50:20 kg/ha"},
        {"name":"Groundnut","icon":"🥜","temp_range":(22,35),"humidity_range":(45,75),"rain_min":5,"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3.5 tonnes/ha","profit":"Rs45,000-70,000/ha","duration":"100-130 days","description":"Excellent oilseed crop for sandy loam soils","soil":"Well-drained light sandy loam","fertilizer":"NPK 25:50:40 kg/ha"},
        {"name":"Potato","icon":"🥔","temp_range":(12,24),"humidity_range":(60,80),"rain_min":0,"season":"Rabi (Winter)","water":"Medium","yield":"25-35 tonnes/ha","profit":"Rs90,000-1,60,000/ha","duration":"80-110 days","description":"High-yielding tuber crop for cool winters","soil":"Deep, friable sandy loam","fertilizer":"NPK 180:80:100 kg/ha"},
        {"name":"Onion","icon":"🧅","temp_range":(15,30),"humidity_range":(50,75),"rain_min":0,"season":"Rabi (Winter)","water":"Medium","yield":"15-25 tonnes/ha","profit":"Rs80,000-1,40,000/ha","duration":"110-140 days","description":"Essential bulb crop with strong market demand","soil":"Deep alluvial or red loams","fertilizer":"NPK 100:50:50 kg/ha"},
        {"name":"Pearl Millet (Bajra)","icon":"🌾","temp_range":(25,40),"humidity_range":(30,65),"rain_min":0,"season":"Kharif (Monsoon)","water":"Low","yield":"2-4 tonnes/ha","profit":"Rs25,000-45,000/ha","duration":"75-90 days","description":"Extremely hardy millet for dry climates","soil":"Light sandy soil","fertilizer":"NPK 80:40:40 kg/ha"},
        {"name":"Sorghum (Jowar)","icon":"🌽","temp_range":(26,38),"humidity_range":(40,70),"rain_min":0,"season":"Kharif (Monsoon)","water":"Low","yield":"2.5-4.5 tonnes/ha","profit":"Rs30,000-50,000/ha","duration":"100-120 days","description":"Drought-resistant grain crop for arid regions","soil":"Deep black loamy soil","fertilizer":"NPK 80:40:40 kg/ha"},
        {"name":"Chili","icon":"🌶️","temp_range":(20,35),"humidity_range":(50,80),"rain_min":0,"season":"Zaid (Summer)","water":"Medium","yield":"2-4 tonnes/ha","profit":"Rs1,00,000-2,00,000/ha","duration":"120-150 days","description":"High-return spice crop for warm seasons","soil":"Rich well-drained loamy soil","fertilizer":"NPK 120:60:60 kg/ha"},
        {"name":"Turmeric","icon":"🫚","temp_range":(20,35),"humidity_range":(65,90),"rain_min":15,"season":"Kharif (Monsoon)","water":"High","yield":"20-30 tonnes/ha","profit":"Rs1,20,000-2,20,000/ha","duration":"240-270 days","description":"Long-duration high value spice crop","soil":"Well-drained loamy or alluvial soil","fertilizer":"NPK 60:50:120 kg/ha"},
    ]
    scored = []
    for crop in all_crops:
        score = 0
        # 1. Temperature score (max 35)
        min_t, max_t = crop["temp_range"]
        if min_t <= temp <= max_t:
            score += 35
        else:
            dist = min(abs(temp - min_t), abs(temp - max_t))
            score += max(0, int(35 - dist * 4))

        # 2. Humidity score (max 25)
        min_h, max_h = crop["humidity_range"]
        if min_h <= humidity <= max_h:
            score += 25
        else:
            dist = min(abs(humidity - min_h), abs(humidity - max_h))
            score += max(0, int(25 - dist * 1.5))

        # 3. Rainfall / Water score (max 20)
        water_req = crop.get("water", "Medium")
        if water_req == "Low":
            score += 20 if rain < 15 else 14
        elif water_req == "Medium":
            score += 20 if 5 <= rain <= 40 else 15
        elif water_req in ("High", "Very High"):
            score += 20 if rain >= crop.get("rain_min", 15) else 10

        # 4. Season match score (max 20)
        if crop["season"] == season:
            score += 20
        elif "Kharif" in crop["season"] and season == "Kharif (Monsoon)":
            score += 20
        elif "Rabi" in crop["season"] and season == "Rabi (Winter)":
            score += 20
        else:
            score += 5

        crop["score"] = min(98, score)
        crop["match"] = f"{crop['score']}%"
        scored.append(crop)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def generate_advisory_calendar(crops):
    today = datetime.now()
    # Determine primary crop's duration range (default ~120 days = 17 weeks)
    primary_crop = crops[0] if crops else {}
    dur_str = primary_crop.get("duration", "90-120 days")
    match_dur = re.findall(r"\d+", dur_str)
    if match_dur:
        avg_days = sum(int(x) for x in match_dur) / len(match_dur)
    else:
        avg_days = 110
    total_weeks = max(8, min(48, int(avg_days / 7)))

    # Scale stages relative to total crop duration
    w_prep   = max(1, int(total_weeks * 0.05))
    w_sow    = max(2, int(total_weeks * 0.10))
    w_irr1   = max(3, int(total_weeks * 0.15))
    w_fert1  = max(4, int(total_weeks * 0.22))
    w_weed   = max(5, int(total_weeks * 0.32))
    w_topd   = max(7, int(total_weeks * 0.45))
    w_pest   = max(9, int(total_weeks * 0.58))
    w_fung   = max(11, int(total_weeks * 0.70))
    w_foliar = max(13, int(total_weeks * 0.82))
    w_stopi  = max(14, int(total_weeks * 0.90))
    w_harv   = total_weeks

    stages = [
        (w_prep,   "Soil preparation & deep ploughing", "preparation"),
        (w_sow,    f"Seed treatment & sowing for {primary_crop.get('name', 'crop')}", "sowing"),
        (w_irr1,   "First post-sowing irrigation", "irrigation"),
        (w_fert1,  f"Apply basal fertilizer ({primary_crop.get('fertilizer', 'NPK')})", "fertilizer"),
        (w_weed,   "First weeding & crop thinning", "maintenance"),
        (w_topd,   "Apply Nitrogen top dressing (Urea)", "fertilizer"),
        (w_pest,   "Inspect fields for early pest/disease infestation", "pesticide"),
        (w_fung,   "Apply preventive fungicide spray if humid", "pesticide"),
        (w_foliar, "Foliar spray of micronutrients & booster", "fertilizer"),
        (w_stopi,  "Stop irrigation 10-14 days before harvesting", "irrigation"),
        (w_harv,   "Harvesting & threshing at full maturity", "harvest"),
    ]

    calendar = []
    for week_num, act, act_type in stages:
        date = today + timedelta(weeks=week_num)
        calendar.append({
            "date":     date.strftime("%d %b %Y"),
            "activity": act,
            "type":     act_type,
            "week":     week_num
        })
    return calendar


def get_pesticide_guide(crops):
    guides = {
        "Rice":                 [{"pest":"Brown Plant Hopper","pesticide":"Imidacloprid 17.8 SL","dose":"125 ml/ha","timing":"At 30 & 60 days after transplanting","eco":False},{"pest":"Leaf folder","pesticide":"Neem Oil 5%","dose":"2.5 L/ha","timing":"At first sign of damage","eco":True}],
        "Wheat":                [{"pest":"Aphids","pesticide":"Dimethoate 30 EC","dose":"1 L/ha","timing":"At tillering stage","eco":False},{"pest":"Yellow rust","pesticide":"Propiconazole 25 EC","dose":"500 ml/ha","timing":"At boot leaf stage","eco":False}],
        "Maize":                [{"pest":"Fall Armyworm","pesticide":"Spinetoram 11.7 SC","dose":"450 ml/ha","timing":"7-10 days after infestation","eco":False},{"pest":"Stem borer","pesticide":"Emamectin Benzoate 5 SG","dose":"220 g/ha","timing":"At whorl stage","eco":False}],
        "Cotton":               [{"pest":"Bollworm","pesticide":"Chlorpyriphos 20 EC","dose":"2.5 ml/L","timing":"At first boll formation","eco":False},{"pest":"Whitefly","pesticide":"Neem Oil 5%","dose":"5 ml/L","timing":"Every 7 days","eco":True}],
        "Tomato":               [{"pest":"Fruit Borer","pesticide":"Flubendiamide 480 SC","dose":"200 ml/ha","timing":"At flowering/fruiting","eco":False},{"pest":"Early Blight","pesticide":"Trichoderma viride 1%","dose":"5 g/L","timing":"Foliar spray at 15-day intervals","eco":True}],
        "Sugarcane":            [{"pest":"Early Shoot Borer","pesticide":"Chlorantraniliprole 18.5 SC","dose":"375 ml/ha","timing":"At 30-45 days after planting","eco":False},{"pest":"Whitefly","pesticide":"Neem cake application","dose":"250 kg/ha","timing":"During soil preparation","eco":True}],
        "Soybean":              [{"pest":"Girdle Beetle","pesticide":"Thiamethoxam 25 WG","dose":"100 g/ha","timing":"At first detection","eco":False},{"pest":"Tobacco Caterpillar","pesticide":"SLNPV Bio-pesticide","dose":"250 LE/ha","timing":"Evening spray on young larvae","eco":True}],
        "Mustard":              [{"pest":"Mustard Aphid","pesticide":"Dimethoate 30 EC","dose":"650 ml/ha","timing":"When 20-30 aphids/plant seen","eco":False},{"pest":"White Rust","pesticide":"Mancozeb 75 WP","dose":"1.5 kg/ha","timing":"Preventive spray at 45 days","eco":False}],
        "Chickpea (Gram)":      [{"pest":"Pod Borer (Helicoverpa)","pesticide":"HaNPV 250 LE/ha","dose":"250 LE/ha","timing":"At flowering stage","eco":True},{"pest":"Wilt","pesticide":"Seed treatment with Trichoderma","dose":"10 g/kg seed","timing":"Before sowing","eco":True}],
        "Groundnut":            [{"pest":"Tikka Leaf Spot","pesticide":"Carbendazim 50 WP","dose":"500 g/ha","timing":"At first symptom appearance","eco":False},{"pest":"White Grub","pesticide":"Beauveria bassiana","dose":"5 kg/ha","timing":"Soil application at sowing","eco":True}],
        "Potato":               [{"pest":"Late Blight","pesticide":"Mancozeb + Metalaxyl","dose":"2.5 g/L","timing":"Prophylactic spray before rains","eco":False},{"pest":"Aphids","pesticide":"Neem oil 10,000 ppm","dose":"2 ml/L","timing":"Weekly spray","eco":True}],
        "Onion":                [{"pest":"Thrips","pesticide":"Fipronil 5 SC","dose":"1.5 ml/L","timing":"When 10 thrips/plant observed","eco":False},{"pest":"Purple Blotch","pesticide":"Copper Oxychloride 50 WP","dose":"3 g/L","timing":"Spray at 15-day intervals","eco":False}],
        "Pearl Millet (Bajra)": [{"pest":"Downy Mildew","pesticide":"Metalaxyl 35 SD","dose":"6 g/kg seed","timing":"Seed treatment before sowing","eco":False},{"pest":"Shoot Fly","pesticide":"Neem seed kernel extract 5%","dose":"500 L/ha","timing":"At 7 and 14 days after emergence","eco":True}],
        "Sorghum (Jowar)":      [{"pest":"Shoot Fly","pesticide":"Imidacloprid 70 WS","dose":"10 g/kg seed","timing":"Seed dressing","eco":False},{"pest":"Stem Borer","pesticide":"Carbofuran 3G granules","dose":"8 kg/ha","timing":"In leaf whorls at 20 days","eco":False}],
        "Chili":                [{"pest":"Chili Thrips & Mites","pesticide":"Spinosad 45 SC","dose":"160 ml/ha","timing":"At peak vector activity","eco":False},{"pest":"Damping Off","pesticide":"Trichoderma harzianum","dose":"10 g/kg seed","timing":"Nursery bed treatment","eco":True}],
        "Turmeric":             [{"pest":"Rhizome Rot","pesticide":"Pseudomonas fluorescens","dose":"10 g/L","timing":"Rhizome dipping before planting","eco":True},{"pest":"Leaf Spot","pesticide":"Mancozeb 75 WP","dose":"2.5 g/L","timing":"Spray at 30-day intervals","eco":False}],
    }
    result = []
    for crop in crops:
        cname = crop.get("name", "")
        if cname in guides:
            result.append({"crop": cname, "guides": guides[cname]})
    return result


# ─── Market Data — Agmarknet (Govt. of India, official) ──────────────────────
# Data source: "Current Daily Price of Various Commodities from Various
# Markets (Mandi)" — published by the Directorate of Marketing & Inspection,
# Ministry of Agriculture & Farmers Welfare, via data.gov.in (open data,
# Govt. of India). This is the SAME data Agmarknet.gov.in itself is built on
# — it is the authoritative, official source for Indian mandi prices, unlike
# global commodity-futures APIs (which price Chicago wheat/corn, not Indian
# mandi produce) or hand-typed reference tables.
#
# Get a free personal key at https://data.gov.in (Sign Up -> My Account ->
# API keys) and set it as DATA_GOV_API_KEY in your .env. Without a key set,
# the app runs on the offline/dynamic MSP-reference fallback below instead
# of ever falling back to any embedded key.
if not DATA_GOV_API_KEY:
    logger.warning("[AgroSmart] DATA_GOV_API_KEY not set — market prices will use the offline "
          "MSP-reference fallback. Get a free personal key at https://data.gov.in")

AGMARKNET_RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
AGMARKNET_URL = f"https://api.data.gov.in/resource/{AGMARKNET_RESOURCE_ID}"

# Reusable session with automatic retries — helps ride out brief network
# hiccups instead of failing on the first slow attempt.
_agmark_session = requests.Session()
_agmark_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
})
_agmark_retry = requests.adapters.Retry(
    total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]
)
_agmark_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=_agmark_retry))

# Agmarknet's commodity names vs. the display names SmartAgro already uses
# in the UI/translations. Extend this as you add more crops.
AGMARK_COMMODITY_ALIASES = {
    "wheat": "Wheat", "rice": "Rice", "maize": "Maize (Corn)",
    "mustard": "Mustard", "groundnut": "Groundnut", "onion": "Onion",
    "potato": "Potato", "tomato": "Tomato", "green chilli": "Chilli",
    "chilli": "Chilli", "sugarcane": "Sugarcane",
    "arhar (tur/red gram)(whole)": "Arhar (Tur)", "arhar": "Arhar (Tur)",
    "green gram (moong)(whole)": "Moong", "moong": "Moong",
    "black gram (urad beans)(whole)": "Urad", "urad": "Urad",
    "soyabean": "Soybean", "soybean": "Soybean", "cotton": "Cotton",
    "jowar(sorghum)": "Jowar (Sorghum)",
    "bajra(pearl millet/cumbu)": "Bajra (Pearl Millet)",
    "bengal gram(gram)(whole)": "Bengal Gram (Chana)",
    "garlic": "Garlic", "ginger(green)": "Ginger", "ginger": "Ginger",
    "turmeric": "Turmeric", "cumin(jeera)": "Cumin (Jeera)",
    "coriander(leaves)": "Coriander", "coriander": "Coriander",
    "banana": "Banana", "mango": "Mango",
}

# Non-crop commodities that show up in the Agmarknet feed (seafood/animal
# products) have no place on a crop-mandi page. Any commodity whose name
# contains one of these keywords is dropped — both when parsing live records
# and again when each city's list is assembled (so stale entries already
# persisted in the price-history cache from before this filter also vanish).
AGMARK_EXCLUDED_COMMODITY_KEYWORDS = ("fish", "prawn", "shrimp", "lobster", "crab")

# Reference prices — the SINGLE source of truth for the offline/MSP-style
# fallback, used both as the static reference table and as the base prices
# get_dynamic_mandi_fallback() varies day-to-day. Used ONLY on the rare day
# a state's mandis haven't reported anything yet (Agmarknet is
# govt.-updated once daily; occasional gaps happen on holidays), or when no
# DATA_GOV_API_KEY is configured at all. Clearly tagged as "msp_fallback" so
# the UI badge tells the farmer these are reference, not live, prices.
#
# NOTE: previously this list existed twice — once here (9 crops, never
# actually read by any code) and again as a separate hardcoded `base_crops`
# list inside get_dynamic_mandi_fallback() (12 crops, including Cotton/
# Soybean/Sugarcane that this list was missing). They're merged into this
# one list so there's exactly one place to add/edit a reference crop price.
MSP_REFERENCE_PRICES = [
    {"crop": "Wheat",               "base_price": 2275},
    {"crop": "Rice",                "base_price": 2183},
    {"crop": "Maize (Corn)",        "base_price": 2090},
    {"crop": "Mustard",             "base_price": 5650},
    {"crop": "Groundnut",           "base_price": 6377},
    {"crop": "Onion",               "base_price": 1800},
    {"crop": "Potato",              "base_price": 1200},
    {"crop": "Tomato",              "base_price": 2500},
    {"crop": "Bengal Gram (Chana)", "base_price": 5440},
    {"crop": "Cotton",              "base_price": 6120},
    {"crop": "Soybean",             "base_price": 4400},
    {"crop": "Sugarcane",           "base_price": 3150},
]


def get_dynamic_mandi_fallback(city):
    """Generate realistic daily mandi prices and 30-day price history trends for a city
    when data.gov.in API key is unconfigured or rate-limited."""
    import hashlib
    today_str = datetime.now().strftime("%Y-%m-%d")

    results = []
    for c in MSP_REFERENCE_PRICES:
        seed_str = f"{city}_{c['crop']}_{today_str}"
        seed_num = int(hashlib.md5(seed_str.encode('utf-8')).hexdigest(), 16)
        pct_var = ((seed_num % 1000) / 1000.0 - 0.45) * 0.06
        price = int(round(c["base_price"] * (1 + pct_var)))
        change = round(pct_var * 100, 2)

        history = []
        for d in range(29, -1, -1):
            day_seed = int(hashlib.md5(f"{city}_{c['crop']}_{d}_{today_str[:7]}".encode('utf-8')).hexdigest(), 16)
            day_var = ((day_seed % 1000) / 1000.0 - 0.48) * 0.05
            history.append(int(round(c["base_price"] * (1 + day_var))))

        results.append({
            "crop": c["crop"],
            "crop_key": c["crop"],
            "price": price,
            "change": change,
            "history": history,
            "unit": "Rs/quintal",
            "source": "msp_fallback",
            "market": f"{city} Mandi",
            "district": CITY_STATE.get(city, city),
            "arrival_date": datetime.now().strftime("%d/%m/%Y"),
        })
    return results


CITY_STATE = {
    "Delhi":         "Delhi",
    "Mumbai":        "Maharashtra",
    "Kolkata":       "West Bengal",
    "Chennai":       "Tamil Nadu",
    "Hyderabad":     "Telangana",
    "Pune":          "Maharashtra",
    "Ahmedabad":     "Gujarat",
    "Lucknow":       "Uttar Pradesh",
    "Jaipur":        "Rajasthan",
    "Bhopal":        "Madhya Pradesh",
    "Patna":         "Bihar",
    "Nagpur":        "Maharashtra",
    "Indore":        "Madhya Pradesh",
    "Surat":         "Gujarat",
    "Kanpur":        "Uttar Pradesh",
    "Coimbatore":    "Tamil Nadu",
    "Visakhapatnam": "Andhra Pradesh",
    "Bhubaneswar":   "Odisha",
    "Guwahati":      "Assam",
    "Amritsar":      "Punjab",
}

# Real daily price history, built up one genuine data point per day as the
# app runs (no fabricated numbers). Persisted to disk so it survives restarts.
_AGMARK_HISTORY_PATH = os.path.join(basedir, "market_history_cache.json")
_agmark_history_lock = threading.Lock()
_agmark_fetch_cache = {}          # {state: (timestamp, results)} — in-memory, 15 min
AGMARK_CACHE_TTL_SEC = 15 * 60


def _load_history_cache():
    try:
        with open(_AGMARK_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history_cache(cache):
    # Write ATOMICALLY (temp file + rename) so a crash mid-write can never leave
    # a truncated/corrupt price-history cache. The read-modify-write cycle is
    # already guarded by _agmark_history_lock at the call site.
    tmp_path = _AGMARK_HISTORY_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _AGMARK_HISTORY_PATH)
    except Exception as e:
        logger.warning(f"[Market] Could not persist history cache: {e}")


def _field(record: dict, *keys):
    """data.gov.in resources don't always serve field names consistently
    (snake_case vs the legacy CKAN 'Modal_x0020_Price' style, or different
    capitalisation) — try every known variant before giving up."""
    for k in keys:
        v = record.get(k)
        if v not in (None, ""):
            return v
    return None


# A handful of states are recorded under a different name than their
# common name (same place, different label) — try each candidate in
# order until one returns records. NOTE: this must only contain true
# synonyms for the same region, never a different-but-nearby state —
# e.g. Telangana and Andhra Pradesh have been separate states since 2014,
# so they are deliberately NOT listed as fallbacks for each other; doing
# so would silently show one state's real prices mislabeled as another's.
STATE_NAME_CANDIDATES = {
    "Delhi":  ["Delhi", "NCT of Delhi"],
    "Odisha": ["Odisha", "Orissa"],
}


def _fetch_state_commodities_raw(state: str) -> dict:
    """HTTP fetch + parse ONLY — deliberately does NOT touch the on-disk
    price-history cache. Returns {display_name: {market, district,
    arrival_date, modal_price}}, or {} if nothing usable was found for this
    state. Safe to call from multiple threads concurrently since it only
    does network I/O, no shared file access."""
    records = []
    for candidate in STATE_NAME_CANDIDATES.get(state, [state]):
        params = {
            "api-key": DATA_GOV_API_KEY,
            "format": "json",
            "limit": 400,
            "filters[state]": candidate,
        }
        try:
            resp = _agmark_session.get(AGMARKNET_URL, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"[Market] Agmarknet HTTP {resp.status_code} for state='{candidate}': {resp.text[:200]}")
                continue
            body = resp.json()
            records = body.get("records", [])
            if records:
                logger.info(f"[Market] Agmarknet: {len(records)} raw records for state='{candidate}' "
                      f"(total available: {body.get('total', '?')})")
                break
            else:
                logger.warning(f"[Market] Agmarknet: 0 records for state='{candidate}' — trying next candidate if any")
        except Exception as e:
            logger.warning(f"[Market] Agmarknet error for state='{candidate}': {e}")
            continue

    if not records:
        logger.warning(f"[Market] Agmarknet: no usable records for {state} after trying all name variants")
        return {}

    # Log the exact keys of the first record once, so if parsing still
    # fails you can see the real field names by checking your app logs.
    logger.info(f"[Market] Sample record keys for {state}: {list(records[0].keys())}")

    # A state has many markets/varieties reporting the same commodity —
    # keep the most recent record per commodity.
    latest_by_commodity = {}
    skipped_no_price = 0
    for r in records:
        raw_name = str(_field(r, "commodity", "Commodity") or "").strip()
        if any(k in raw_name.lower() for k in AGMARK_EXCLUDED_COMMODITY_KEYWORDS):
            skipped_no_price += 1  # not a crop (e.g. fish) — exclude
            continue
        modal = _field(r, "modal_price", "Modal_x0020_Price", "Modal Price", "modal price")
        if not raw_name or modal is None:
            skipped_no_price += 1
            continue
        try:
            modal_price = float(modal)
        except (TypeError, ValueError):
            skipped_no_price += 1
            continue
        if modal_price <= 0:
            continue
        display_name = AGMARK_COMMODITY_ALIASES.get(raw_name.lower(), raw_name.title())
        latest_by_commodity[display_name] = {
            "market":       _field(r, "market", "Market") or "",
            "district":     _field(r, "district", "District") or "",
            "arrival_date": _field(r, "arrival_date", "Arrival_Date") or "",
            "modal_price":  modal_price,
        }

    logger.info(f"[Market] {state}: parsed {len(latest_by_commodity)} commodities, "
          f"skipped {skipped_no_price} records (missing/invalid price or name)")
    return latest_by_commodity


def fetch_agmarknet_prices_bulk(states: list) -> dict:
    """Fetch REAL, government-reported mandi prices for MULTIPLE states at
    once. HTTP calls still run in parallel (one thread per state), but the
    on-disk price-history cache is now read ONCE, updated in memory for
    every state in this batch, and written ONCE — instead of the old
    behaviour where every parallel worker thread independently read and
    rewrote the *entire* history file for its own single state. With ~13
    states that used to mean 13 full file reads + 13 full file rewrites per
    /api/market call; this collapses it to 1 read + 1 write per call.
    Returns {state: [crop_result, ...]}."""
    if not DATA_GOV_API_KEY:
        return {s: [] for s in states}

    now = time.monotonic()
    results_by_state = {}
    states_to_fetch = []
    for s in states:
        cached = _agmark_fetch_cache.get(s)
        if cached and (now - cached[0]) < AGMARK_CACHE_TTL_SEC:
            results_by_state[s] = cached[1]
        else:
            states_to_fetch.append(s)

    if not states_to_fetch:
        return results_by_state  # everything served from cache — zero file I/O this call

    # ── Phase 1: HTTP fetch + parse, in parallel, no shared file access ──
    raw_by_state = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(states_to_fetch) or 1)) as executor:
        future_to_state = {executor.submit(_fetch_state_commodities_raw, s): s for s in states_to_fetch}
        for future in concurrent.futures.as_completed(future_to_state):
            state = future_to_state[future]
            try:
                raw_by_state[state] = future.result()
            except Exception as e:
                logger.warning(f"[Market] Unexpected error fetching {state}: {e}")
                raw_by_state[state] = {}

    # ── Phase 2: ONE load, update every state in memory, ONE save ───────
    today_key = datetime.now().strftime("%Y-%m-%d")
    with _agmark_history_lock:
        cache = _load_history_cache()  # single read for the whole batch

        for state in states_to_fetch:
            latest_by_commodity = raw_by_state.get(state) or {}
            state_hist = cache.setdefault(state, {})
            state_results = []

            for display_name, rec in latest_by_commodity.items():
                hist = state_hist.setdefault(display_name, [])
                if not hist or hist[-1].get("date") != today_key:
                    hist.append({"date": today_key, "price": rec["modal_price"]})
                    hist[:] = hist[-30:]  # keep the last 30 real daily points

                prev_price = hist[-2]["price"] if len(hist) > 1 else rec["modal_price"]
                change = round(((rec["modal_price"] - prev_price) / prev_price) * 100, 2) if prev_price else 0.0

                state_results.append({
                    "crop":         display_name,
                    "crop_key":     display_name,
                    "price":        int(round(rec["modal_price"])),
                    "change":       change,
                    "history":      [h["price"] for h in hist] or [rec["modal_price"]],
                    "unit":         "Rs/quintal",
                    "source":       "agmarknet_live",
                    "market":       rec["market"],
                    "district":     rec["district"],
                    "arrival_date": rec["arrival_date"],
                })

            results_by_state[state] = state_results
            _agmark_fetch_cache[state] = (now, state_results)
            logger.info(f"[Market] Agmarknet OK for {state}: {len(state_results)} commodities")

        _save_history_cache(cache)  # single write for the whole batch

    return results_by_state


def fetch_agmarknet_prices(state: str) -> list:
    """Single-state convenience wrapper, kept for backward compatibility.
    Internally delegates to the batched fetch — prefer
    fetch_agmarknet_prices_bulk() directly when fetching multiple states,
    since calling this in a loop would go back to one file I/O cycle per
    call."""
    if not DATA_GOV_API_KEY:
        return []
    return fetch_agmarknet_prices_bulk([state]).get(state, [])


def get_demand(change: float, price_deviation_pct: float) -> str:
    """Blend two independent signals so demand actually varies:

    1. `change` — real day-over-day price movement (from Agmarknet history).
       This alone is structurally flat on the FIRST data point recorded
       each day: with only one point in history, prev_price == current
       price, so change is 0.0 for literally every commodity that day —
       which is why every crop used to show "Medium" until history had
       accumulated a few days.
    2. `price_deviation_pct` — how this market's price compares to the
       average price for the same commodity across every other market in
       the same response, right now. This signal exists from day one
       (it doesn't need historical data at all), so it's what keeps
       demand meaningfully different across cities/crops even before
       day-over-day trends have had time to build up.
    """
    # ── Realistic demand scoring ──────────────────────────────────────────
    # Day-over-day change is the dominant signal in a real mandi: falling
    # prices mean surplus / weak buying (LOW demand), rising prices mean
    # buyers competing (HIGH demand). The premium vs the cross-market average
    # is only a tie-breaker. Both inputs are clamped so one wild data point
    # (e.g. a -10% crash or a broken +40% premium) can NEVER flip the result
    # to "Very High demand" — a falling price is never high demand.
    c = max(-12.0, min(12.0, float(change if change is not None else 0.0)))
    d = max(-8.0, min(8.0, float(price_deviation_pct if price_deviation_pct is not None else 0.0)))
    score = 2.0 * c + 0.5 * d
    if score > 7:     return "Very High"   # strong rally AND/OR above-average price
    elif score > 2:   return "High"
    elif score >= -2: return "Medium"
    else:             return "Low"         # falling or deeply discounted price


@app.route('/api/market')
def get_market_data():
    cities = list(CITY_STATE.keys())
    location = request.args.get('location', '').strip().lower()
    if location:
        cities = [c for c in cities if location in c.lower()]

    markets = {}
    live_total = 0
    static_total = 0

    # Fetch every unique state IN PARALLEL instead of one-by-one — with
    # ~13 unique states and a government API that can be slow/overloaded,
    # doing this sequentially could mean the whole page waits 12s x 13
    # states in the worst case. Parallel fetching caps total wait time to
    # roughly one slowest request instead of the sum of all of them.
    # fetch_agmarknet_prices_bulk() does the HTTP fan-out itself AND
    # batches the price-history file read/write into a single cycle for
    # the whole call, instead of one file read + write per state thread.
    unique_states = sorted({CITY_STATE.get(c, "") for c in cities})
    state_results_cache = fetch_agmarknet_prices_bulk(unique_states)

    # Pull the raw per-city crop lists first (no demand assigned yet) so
    # we can compute a cross-market average price per commodity BEFORE
    # scoring demand for any individual city.
    city_crop_lists = {}
    for city in cities:
        state = CITY_STATE.get(city, "")
        crops = list(state_results_cache.get(state, []))
        if not crops:
            crops = get_dynamic_mandi_fallback(city)
        # Drop any non-crop commodity (fish/prawn etc.) — this also cleans out
        # stale entries that were persisted in the price-history cache before
        # the exclusion filter existed.
        crops = [
            c for c in crops
            if not any(k in str(c.get("crop", "")).lower() for k in AGMARK_EXCLUDED_COMMODITY_KEYWORDS)
        ]
        city_crop_lists[city] = crops

    price_sum = {}
    price_count = {}
    for crops in city_crop_lists.values():
        for c in crops:
            key = c.get("crop_key") or c["crop"]
            price_sum[key] = price_sum.get(key, 0) + c["price"]
            price_count[key] = price_count.get(key, 0) + 1
    avg_price_by_crop = {k: price_sum[k] / price_count[k] for k in price_sum}

    for city in cities:
        city_crops = []
        for crop in city_crop_lists[city]:
            key = crop.get("crop_key") or crop["crop"]
            avg = avg_price_by_crop.get(key) or crop["price"]
            deviation_pct = ((crop["price"] - avg) / avg) * 100 if avg else 0.0
            demand = get_demand(crop["change"], deviation_pct)
            city_crops.append({**crop, "demand": demand})

        city_crops.sort(
            key=lambda x: ({"Very High": 3, "High": 2, "Medium": 1, "Low": 0}.get(x["demand"], 0), x["price"]),
            reverse=True
        )
        markets[city] = city_crops
        live_total   += sum(1 for c in city_crops if c.get("source") == "agmarknet_live")
        static_total += sum(1 for c in city_crops if c.get("source") != "agmarknet_live")

    return jsonify({
        "markets":      markets,
        "locations":    list(markets.keys()),
        "live_count":   live_total,
        "static_count": static_total,
        "fetched_at":   datetime.now().isoformat(),
        "data_source":  "Agmarknet — Ministry of Agriculture & Farmers Welfare, Govt. of India (data.gov.in)",
    })


# ─── Debug endpoint ───────────────────────────────────────────────────────────
@app.route('/api/debug-market')
def debug_market():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    if not DATA_GOV_API_KEY:
        return jsonify({"error": "DATA_GOV_API_KEY not set in .env"}), 500
    state = request.args.get('state', 'Delhi')
    try:
        resp = _agmark_session.get(
            AGMARKNET_URL,
            params={"api-key": DATA_GOV_API_KEY, "format": "json", "limit": 20, "filters[state]": state},
            timeout=15
        )
        if not resp.ok:
            return jsonify({"http_status": resp.status_code, "raw_response": resp.text[:1000]})
        body = resp.json()
        records = body.get("records", [])
        return jsonify({
            "http_status":      resp.status_code,
            "total_available":  body.get("total"),
            "records_returned": len(records),
            "sample_record":    records[0] if records else None,
            "sample_keys":      list(records[0].keys()) if records else [],
            "note": "If records_returned is 0, try a different 'state' value (e.g. ?state=Maharashtra). "
                    "If sample_record exists but crops still don't show on /market, compare sample_keys "
                    "against the field names read in fetch_agmarknet_prices().",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Kisan Helper Chatbot ─────────────────────────────────────────────────────
CHAT_LIMIT  = 20

# ─── Shared sliding-window rate limiter ───────────────────────────────────────
# Chat, STT, diagnosis, and translation endpoints each throttle per-IP with the
# SAME sliding-window logic. One shared implementation (instead of four copies)
# plus a single lock keeps behaviour identical and thread-safe. The state lives
# in-process, so it is only consistent under a SINGLE gunicorn worker
# (see Dockerfile: --workers 1 --threads 8); multi-worker scaling needs Redis.
_rate_limit_state = {}         # action -> {ip: [timestamps]}
_rate_limit_state_lock = threading.Lock()

def _rate_limit(action: str, ip: str, limit: int, window_seconds: int = 60) -> bool:
    """Sliding-window rate limit. Returns True if the request is allowed,
    False if it exceeds `limit` within `window_seconds`. Thread-safe."""
    now = datetime.now().timestamp()
    with _rate_limit_state_lock:
        bucket = _rate_limit_state.setdefault(action, {})
        times = [t for t in bucket.get(ip, []) if now - t < window_seconds]
        if len(times) >= limit:
            bucket[ip] = times
            return False
        times.append(now)
        bucket[ip] = times
        return True


def _is_rate_limited(ip: str) -> bool:
    return not _rate_limit("chat", ip, CHAT_LIMIT)


# ─── Chatbot topic gate (#chat-limits) ──────────────────────────────────────
# Kisan Helper is intentionally narrow-scoped: only agriculture / SmartAgro.
# These helpers keep random chit-chat (jokes, general knowledge, coding help,
# …) out WITHOUT burning a billed Groq call or wasting the model's context.

# Strong on-topic signals. If ANY appears in a message we skip the classifier
# entirely — this saves latency and tokens on the flood of genuine farmer
# questions every day. Covers English + the most common Indian languages.
_CHAT_ON_TOPIC_WORDS = [
    "crop", "crops", "farming", "farmer", "farm", "soil", "seed", "seeds",
    "fertiliser", "fertilizer", "pesticide", "insecticide", "fungicide", "herbicide",
    "irrigation", "irrigate", "rain", "rainfall", "weather", "temperature", "humidity",
    "market", "mandi", "price", "prices", "yield", "harvest", "harvesting", "sowing",
    "plant", "plants", "leaf", "leaves", "disease", "diseases", "pest", "pests",
    "insect", "insects", "bug", "bugs", "weed", "weeds", "field", "fields",
    "paddy", "rice", "wheat", "maize", "corn", "cotton", "sugarcane",
    "groundnut", "peanut", "soybean", "soyabean", "sunflower", "mustard", "onion",
    "potato", "tomato", "chilli", "chili", "ginger", "turmeric", "garlic",
    "neem", "compost", "manure", "urea", "drip", "sprinkler", "drainage",
    "monsoon", "rabi", "kharif", "zaid", "germination", "sapling", "transplant",
    "maturity", "ripening", "pm-kisan", "pmkisan", "scheme", "subsidy", "loan",
    "insurance", "kcc", "organic", "biofertilizer", "biopesticide",
    "फसल", "खेती", "किसान", "मिट्टी", "बीज", "उर्वरक", "कीटनाशक", "सिंचाई", "बारिश",
    "मौसम", "तापमान", "आर्द्रता", "बाजार", "भाव", "उपज", "कटाई", "बोना", "पौधा",
    "पत्ता", "रोग", "कीट", "धान", "गेहूँ", "मक्का", "कपास", "गन्ना", "मूँगफली",
    "सरसों", "प्याज", "आलू", "टमाटर", "मिर्च", "अदरक", "हल्दी", "लहसन", "नीम",
    "खाद", "यूरिया", "ड्रिप", "मानसून", "सरकार", "योजना",
    "ਖੇਤੀ", "ਫਸਲ", "किसान", "ખેતી", "ફસલ", "शेती", "సాగుదిద్ది", "விவசாய்",
    "பயிர்", "ಕೃಷಿ", "ಬೆಳೆ", "কৃষি", "ফসল", "বাংলা",
    "agriculture", "agri", "horticulture", "mulch", "tillage", "plough", "tractor",
    "thresher", "pruning", "grafting", "nursery", "greenhouse", "polyhouse", "orchard",
    "plantation", "seedling", "variety", "hybrid", "nutrient", "nitrogen", "phosphorus",
    "potassium", "micronutrient", "zinc", "boron", "vermicompost", "pheromone", "blight",
    "rust", "wilt", "mildew", "nematode", "mango", "banana", "pomegranate", "guava",
    "papaya", "coconut", "cashew", "rubber", "tea", "coffee", "cardamom", "pepper",
    "chickpea", "gram", "lentil", "pulse", "pulses", "barley", "oats", "millet",
    "bajra", "jowar", "ragi", "sorghum", "sesame", "til", "castor", "jute",
    "drought", "waterlogging", "frost", "hail", "sunlight", "intercropping",
    "crop rotation", "cover crop", "green manure", "raised bed", "furrow", "bund",
    "terrace", "deficiency", "chlorosis", "necrosis", "trichoderma", "rhizobium",
    "azotobacter", "biocontrol", "ladybug", "spray", "curl", "yellowing", "photosynthesis",
]
_CHAT_OFF_TOPIC_WORDS = [
    "movie", "movies", "cricket", "football", "song", "songs", "joke", "jokes",
    "celebrity", "actor", "actress", "politics", "stock", "stocks", "crypto",
    "bitcoin", "software", "coding", "programming", "resume", "job", "jobs",
    # Same junk in Indian scripts, so the fast-path works without a classifier
    # call regardless of the user's language.
    "सिनेमा", "फिल्म", "मज़ाक", "मजाक", "क्रिकट", "फुटबॉल", "गाना", "गीत",
    "कोडिंग", "प्रोग्रामिंग", "नर्तक", "अभिनय", "राजनीति",
    "recipe", "recipes", "cooking", "cook", "kitchen", "restaurant", "travel", "tour",
    "tourism", "holiday", "vacation", "instagram", "facebook", "whatsapp", "youtube", "tiktok",
    "reels", "gaming", "gamer", "playstation", "xbox", "computer", "laptop", "mobile",
    "smartphone", "internet", "website", "technology", "science", "physics", "chemistry",
    "maths", "mathematics", "history", "geography", "news", "fashion", "clothes", "shopping",
    "makeup", "fitness", "gym", "doctor", "hospital", "medicine", "car", "bike", "vehicle",
    "electricity", "engineering", "law", "court", "lawyer", "hockey", "tennis", "badminton",
    "kabaddi", "chess", "olympics", "ipl", "film", "cinema", "serial", "drama", "music",
    "dance", "singing", "art", "painting", "drawing", "poem", "poetry", "story", "novel",
    "book", "literature", "philosophy", "horoscope", "astrology", "astronomy", "space",
    "rocket", "university", "college", "school", "exam", "homework", "salary", "voting",
    "election", "crime", "police", "love", "relationship", "marriage", "birthday",
    "celebration", "party",
    "खाना", "रेसिपी", "यात्रा", "खेल", "गेम", "पढ़ाई", "दवा", "बीमारी", "प्रेम", "शादी", "पार्टी",
    "debug", "code", "codes", "python", "javascript", "html", "css", "github",
    "capital", "country", "population", "president", "moon", "planet", "universe", "galaxy",
]


def _compile_word_matchers(words):
    """Split a keyword list into (a) Latin words matched as WHOLE words with an
    optional English plural -(e)s, and (b) Indian-script words matched as plain
    substrings (so plurals like किसानों still carry the stem किसान). Whole-word
    matching stops substring collisions such as 'debug' being treated as the
    crop keyword 'bug', or 'program' as the crop 'gram'."""
    latin = [w for w in words if w.isascii()]
    scripts = tuple(w for w in words if not w.isascii())
    rx = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in latin) + r")(?:s|es)?\b", re.IGNORECASE)
    return rx, scripts


_CHAT_ON_TOPIC_RX, _CHAT_ON_TOPIC_RAW = _compile_word_matchers(_CHAT_ON_TOPIC_WORDS)
_CHAT_OFF_TOPIC_RX, _CHAT_OFF_TOPIC_RAW = _compile_word_matchers(_CHAT_OFF_TOPIC_WORDS)


def _chat_message_on_topic(text: str) -> bool:
    """Decide whether `text` is plausibly an agriculture/SmartAgro question.
    Fast keyword fast-path; falls back to a tiny Groq classifier. FAIL-OPEN."""
    if not text:
        return True
    low = text.lower()
    # 1) clear agricultural keyword present -> on topic, no classifier needed
    if _CHAT_ON_TOPIC_RX.search(low):
        return True
    if any(w in low for w in _CHAT_ON_TOPIC_RAW):
        return True
    # 2) clear off-topic keyword present (and no agri signal) -> off topic,
    #    no classifier needed. This blocks obvious junk/cheerio prompts
    #    without a network round-trip.
    if _CHAT_OFF_TOPIC_RX.search(low):
        return False
    if any(w in low for w in _CHAT_OFF_TOPIC_RAW):
        return False
    # 3) generic/ambiguous message -> cheap throwaway classifier pass
    if not GEMINI_API_KEY:
        return True  # fail open
    headers = _ai_headers()
    body = {
        "model": AI_CHAT_MODEL,
        "messages": [
            {"role": "user", "content": (
                "You are a strict content filter for Kisan Helper, a farming-only chatbot for Indian farmers. "
                "Classify whether the message is about FARMING/AGRICULTURE or the SmartAgro app: crop diseases & pests, "
                "soil, weather for crops, irrigation, seeds, fertilizers/pesticides, market/mandi prices, or government "
                "farm schemes/subsidies (PM-KISAN, KCC, crop insurance). "
                "If it is about one of those, output {\"on_topic\": true}. "
                "If it is about anything else — general knowledge, news, movies, sports, jokes, coding, math, science, "
                "recipes, travel, greetings/chit-chat, or personal/health advice — output {\"on_topic\": false}. "
                "Examples: \"my wheat leaves are yellow\" → {\"on_topic\": true}; \"what is the capital of France\" → {\"on_topic\": false}; "
                "\"tell me a joke\" → {\"on_topic\": false}; \"hello how are you\" → {\"on_topic\": false}. "
                "Reply with ONLY the JSON object, nothing else.\n\nMessage: " + text[:400]
            )}
        ],
        "temperature": 0,
        "max_tokens": 32,
    }
    try:
        resp = requests.post(AI_COMPLETIONS_URL, headers=headers, json=body, timeout=8)
        if resp.status_code != 200:
            return True  # fail open
        parsed = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        parsed = re.sub(r"```(?:json)?", "", parsed).replace("```", "").strip()
        m = re.search(r"\{.*\}", parsed, re.DOTALL)
        obj = json.loads(m.group() if m else parsed)
        return bool(obj.get("on_topic", True))
    except Exception:
        return True  # fail open — never block a real farmer question


# Pre-canned polite refusals (localized) for the off-topic case, so we don't
# pay for a model call to generate a redirect. Covers the major Indian
# languages; falls back to English.
_OFF_TOPIC_REPLIES = {
    "en":  "🌾 I'm Kisan Helper — I only know about farming! Ask me about crops, pests, soil, weather, market prices, or schemes like PM-KISAN, and I'll help. ",
    "hi":  "🌾 मैं Kisan Helper हूँ — मुझे सिर्फ खेती के बारे में पता चलता है! फसल, कीट, मिट्टी, मौसम, बाजार भाव या सरकारी योजनाओं के बारे में पूछिए। ",
    "bn":  "🌾 আমি Kisan Helper — আমি স্বল্প জানি কৃষি সম্পর্কে! ফসল, কীট, মাটি, আবহাওয়া, বাজার দাম বা সরকারি ষ্ট্যাম্প সম্পর্কে জিজ্ঞাসা করুন। ",
    "pa":  "🌾 ਮੈਂ Kisan Helper ਹਾਂ — ਮੈਂ ਸਿਰਫ਼ ਖੇਤੀਬਾੜੀ ਬਾਰੇ ਜਾਣਦਾ ਹਾਂ! ਫਸਲ, ਕੀੜੇ, ਮਿੱਟੀ, ਮੌਸਮ ਜਾਂ ਸਰਕਾਰੀ ਯੋਜਨਾਵਾਂ ਬਾਰੇ ਪੁੱਛੋ। ",
    "gu":  "🌾 હું Kisan Helper છું — હું માત્ર ખેતી વિશે જાણું છું! પંક્જન, કીટ, માટી, હવામાન કે સરકારી યોજનાઓ વિશે પૂછો. ",
    "mr":  "🌾 मी Kisan Helper आहे — मला फक्त शेतीवरून माहिती आहे! पीळ, कीड, माती, हवामान किंवा सरकारी योजना विषयी विचारा. ",
    "ta":  "🌾 நான் Kisan Helper — வயவியில் மேல்ல தெரிவிகை! பயிர், நுண்ணின்பை, மண், வானிலை அல்லது அரசுக் கொள்கைகள் பற்றி கேளுங்கள். ",
    "te":  "🌾 నేను Kisan Helper — నాణ్యమైన వ్యవసాయం మాత్రమే! పంటలు, నిత్యకూలి, నేల, వాతావరణం లేదా ప్రభుత్వ యోజనల గురించి అడగండి. ",
    "ml":  "🌾 ഞാൻ Kisan Helper — കർഷണം സംബന്ധിച്ചതുൾപ്പറമ്മേ! ചെറ്റം, പീഡി, മണ്ണ് അല്ലെങ്കിൽ സർക്കാർ പദ്ധതി എന്നിവ ചോദിക്കുക. ",
    "kn":  "🌾 ನಾನು Kisan Helper — ರೈವಸಾಯಿಕೆ ಸಂಬಂಧಿಸಿದೆಯೇ! ಬೆಳಿ, ಕೇಂದ್ರ, ನೊಂತಳ್ಳಿ, ಹವಾಮನ ಅಥವಾ ಸರ್ಕಾರಿ ಯೋಜನೆಗಳ ಬಗ್ಗೆ ಕೇಳಿ. ",
    "or":  "🌾 ମୁଁ Kisan Helper — କୃଷି ବିଷୟରୁ ଜାଣାକର୍ତ୍ତା! ଫସଲ, କୀଟ, ମାଟି, ମୌସମ କିମ୍ବା ସରକାରୀ ଯୋଜନା ରେ ପ୍ରଶ୍ନ କରନ୍ତୁ। ",
    "as":  "🌾 মই Kisan Helper — মৌলো কৃষি সম্পৰ্কে জাণো! ফসল, কীট, মাটি, আবহাওয়া অথবা সৰকাৰী যোজনাৰ বিষয়ে উত্থান কৰিও। ",
    "sa":  "🌾 अहं Kisan Helper अस्मि — कृषिविषये एव सहाययाम्! फसलं, कीटः, माटि:, मौसमः इत्यादी पृष्टु। ",
}

def _off_topic_reply(lang: str) -> str:
    return _OFF_TOPIC_REPLIES.get(lang, _OFF_TOPIC_REPLIES["en"])


# ── Kisan Helper Feature Gateway ───────────────────────────────────────────
# The chatbot is the single entry point for EVERY SmartAgro feature. When a
# farmer asks about weather, alerts, market prices, crop recommendations,
# disease diagnosis or seasonal advice, these helpers detect the intent, run
# the REAL feature (the same functions the page routes use), and inject the
# live results into the LLM context so the reply is grounded in actual data.
# Interactive features (diagnosis, full-page views) also emit "action chips"
# that the frontend renders as one-tap navigation buttons.

_CHAT_FEATURE_KEYWORDS = {
    "weather": [
        "weather", "mausam", "मौसम", "temp", "temperature", "temprature",
        "rain", "rainfall", "barish", "बारिश", "sunny", "धूप", "thunder",
        "storm", "wind", "hawa", "हवा", "forecast", "mosam",
    ],
    "alerts": [
        "alert", "warning", "risk", "danger", "खतरा", "savdhan", "सावधान",
        "pest", "कीट", "fungus", "fungal", "blight", "harmful",
        "attack", "hamla", "हमला",
    ],
    "market": [
        "price", "prices", "mandi", "मंडी", "market", "bhav", "भाव",
        "rate", "rates", "cost", "demand", "sell", "buy", "commodity",
        "ky rate", "kray",
    ],
    "crops": [
        "recommend", "suggest", "suitable", "what to grow", "which crop",
        "best crop", "kaun si fasal", "कौन सी फसल", "fasal", "फसल",
        "grow", "उगाएं", "boon", "बुवाई",
    ],
    "diagnosis": [
        "diagnose", "disease", "बीमारी", "rogi", "रोग", "leaf", "पत्ता",
        "पत्ती", "spot", "धब्बा", "wilt", "मुरझाया", "rot", "सड़न",
        "yellow leaf", "पीली पत्ती", "upload", "photo", "image",
        "symptoms", "infection", "लक्षण",
    ],
    "seasonal": [
        "season", "kharif", "rabi", "zaid", "advisory", "monthly",
        "calendar", "sowing", "harvest season", "मौसमी", "सलाह",
    ],
}

_FEATURE_LABEL_MAP = {
    "weather":   ("🌤️ Weather", "/"),
    "alerts":    ("⚠️ Alerts", "/alerts"),
    "market":    ("🏪 Market Prices", "/market"),
    "diagnosis": ("📷 Diagnose Crop", "/diagnose"),
    "crops":     ("🌾 Crop Suggestions", "/"),
    "seasonal":  ("🗓️ Seasonal Advice", "/alerts"),
}

_RECOGNISED_CITIES = sorted(CITY_STATE.keys(), key=len, reverse=True)


# Crop commodities the market tool can pin-point in a question, mapped to the
# exact display names used by the market data (MSP_REFERENCE_PRICES / Agmarknet).
# English/Latin keywords match on word boundaries so "rice" never matches inside
# "price"; Indian-script words use substring matching to catch inflected forms.
_CHAT_COMMODITY_KEYWORDS = {
    "Wheat":                ["wheat", "गेहूं", "गेहूँ", "gehu", "kannak", "कनक"],
    "Rice":                 ["rice", "paddy", "dhaan", "चावल", "धान"],
    "Maize (Corn)":         ["maize", "corn", "makka", "मक्का", "bhutta"],
    "Mustard":              ["mustard", "sarson", "सरसों", "rai"],
    "Groundnut":            ["groundnut", "peanut", "moongfali", "मूंगफली"],
    "Onion":                ["onion", "pyaaz", "प्याज", "kanda"],
    "Tomato":               ["tomato", "tamatar", "टमाटर"],
    "Potato":               ["potato", "aloo", "aalu", "आलू"],
    "Bengal Gram (Chana)":  ["chana", "channa", "bengal gram", "चना"],
    "Cotton":               ["cotton", "kapas", "कपास", "rui"],
    "Soybean":              ["soybean", "soya", "soyabean", "सोयाबीन"],
    "Sugarcane":            ["sugarcane", "ganna", "गन्ना"],
    "Pearl Millet (Bajra)": ["bajra", "pearl millet", "बाजरा"],
    "Sorghum (Jowar)":      ["jowar", "sorghum", "ज्वार"],
    "Chili":                ["chili", "chilli", "mirchi", "mirch", "मिर्च"],
    "Turmeric":             ["turmeric", "haldi", "हल्दी"],
}

# City aliases so older/informal English names still resolve correctly.
_CHAT_CITY_ALIASES = {
    "calcutta":  "Kolkata",
    "bombay":    "Mumbai",
    "madras":    "Chennai",
    "bangalore": "Bengaluru",
    "new delhi": "Delhi",
}


def _chat_detect_commodity(text):
    """Find the crop/commodity the farmer is asking about (e.g. 'wheat',
    'गेहूं', 'tomato'). Returns the market display name, or '' if none."""
    import re
    if not text:
        return ""
    t = " " + (text or "").lower() + " "
    for name, kws in _CHAT_COMMODITY_KEYWORDS.items():
        for kw in kws:
            if kw.isascii():
                if re.search(r"\b" + re.escape(kw) + r"\b", t):
                    return name
            else:
                if kw in t:
                    return name
    return ""


def _chat_detect_city(text):
    """Find an app-known city name inside the message so location-specific
    features (market, seasonal advice) can target the right place."""
    if not text:
        return ""
    t = text.lower()
    for alias, city in _CHAT_CITY_ALIASES.items():
        if alias in t:
            return city
    for city in _RECOGNISED_CITIES:
        if city.lower() in t:
            return city
    return ""


def _chat_detect_intents(text):
    """Map a message to the SmartAgro features it needs.
    Latin keywords use word boundaries (so 'rain' never matches 'drainage').
    Indian-script keywords use substring matching — Devanagari/other scripts
    encode inflection with combining marks that \b treats as non-word chars
    (e.g. फसलों / फसले would break \bफसल\b), while a plain substring cleanly
    catches all the inflected forms of a root word."""
    import re
    t = (text or "").lower()
    padded = " " + t + " "
    matched = set()
    for feature, kws in _CHAT_FEATURE_KEYWORDS.items():
        for kw in kws:
            if kw.isascii():
                if re.search(r"\b" + re.escape(kw) + r"\b", padded):
                    matched.add(feature)
                    break
            else:
                if kw in t:
                    matched.add(feature)
                    break
    return matched

# ── Gateway tool implementations ────────────────────────────────────────────
# Each tool runs the exact same functions the page routes use, so a farmer
# asking the chatbot gets LIVE numbers — never a stale guess.

def _chat_weather_tool(lat, lon):
    if not lat or not lon:
        return {"ok": False, "reason": "no_location"}
    w = _fetch_current_weather(float(lat), float(lon))
    if not w:
        return {"ok": False, "reason": "unavailable"}
    cur, fc = w.get("current") or {}, w.get("forecast") or []
    f_str = "; ".join(
        f"{d.get('date')}: {d.get('temp_max')}°C/{d.get('temp_min')}°C, "
        f"{d.get('humidity')}% RH, {d.get('rain')}mm rain, {d.get('description')}"
        for d in fc[:5] if isinstance(d, dict)
    )
    return {
        "ok": True,
        "city": cur.get("city"),
        "temp_c": cur.get("temp"),
        "humidity_pct": cur.get("humidity"),
        "description": cur.get("description"),
        "wind_mps": cur.get("wind_speed"),
        "rain_mm": cur.get("rain"),
        "forecast_text": f_str,
    }


def _chat_alerts_tool(lat, lon):
    if not lat or not lon:
        return {"ok": False, "reason": "no_location"}
    w = _fetch_current_weather(float(lat), float(lon))
    if not w:
        return {"ok": False, "reason": "unavailable"}
    cur = w.get("current") or {}
    alerts_list = _rule_alerts(
        float(cur.get("temp", 25)),
        float(cur.get("humidity", 60)),
        float(cur.get("wind_speed", 10)),
        float(cur.get("rain", 0)),
        cur.get("description", ""),
    )
    return {
        "ok": True,
        "city": cur.get("city"),
        "count": len(alerts_list),
        "alerts": [
            {"title": a["title"], "message": a["message"],
             "action": a["action"], "category": a["category"]}
            for a in alerts_list
        ],
    }


def _chat_crops_tool(lat, lon, city):
    season = get_season(datetime.now().month)
    if not lat or not lon:
        base = recommend_crops(25, 60, 0, season)
        return {
            "ok": True, "season": season, "city": city or "your area",
            "crops": [{"name": c.get("name"), "match": c.get("match")}
                      for c in (base or [])][:6],
        }
    w = _fetch_current_weather(float(lat), float(lon))
    do_temp = float(w["current"]["temp"]) if w else 25
    do_hum  = float(w["current"]["humidity"]) if w else 60
    do_rain = float(w["current"].get("rain") or 0) if w else 0
    crops = ai_recommend_crops(city or "", float(lat), float(lon),
                                do_temp, do_hum, do_rain, season)
    if not crops:
        crops = recommend_crops(do_temp, do_hum, do_rain, season) or []
    return {
        "ok": True, "season": season, "city": city or "",
        "crops": [{"name": c.get("name"), "match": c.get("match"),
                    "description": c.get("description")}
                  for c in crops[:6] if isinstance(c, dict)],
    }


def _chat_fetch_market_rows(city, state):
    """Fastest mandi rows available for a city:
       1. fresh in-memory Agmarknet cache (instant),
       2. bounded live Agmarknet fetch — max ~6 s so the chat never stalls,
       3. deterministic MSP-reference fallback (always works, even offline)."""
    if state:
        cached_state = _agmark_fetch_cache.get(state)
        if cached_state and (time.monotonic() - cached_state[0]) < AGMARK_CACHE_TTL_SEC:
            rows = list(cached_state[1])
            if rows:
                return rows
    if state and DATA_GOV_API_KEY:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(fetch_agmarknet_prices_bulk, [state])
            try:
                state_result = fut.result(timeout=6)
                rows = list(state_result.get(state, []))
                if rows:
                    return rows
            except Exception as exc:
                logger.warning(f"[ChatMarket] live fetch failed for {state}: {exc}")
    return list(get_dynamic_mandi_fallback(city) or [])


def _chat_market_tool(city, commodity=""):
    """Live mandi prices for a city (fast path → live path → offline fallback).
    If the farmer named a commodity ('wheat', 'गेहूं'), `focus` lists its rows
    first so the reply can quote the exact price they asked about."""
    if not city:
        return {"ok": False, "reason": "no_city"}
    state = CITY_STATE.get(city, "")
    rows = _chat_fetch_market_rows(city, state)
    items = [
        {"crop": c.get("crop"), "price": c.get("price"),
         "change_pct": c.get("change", 0), "source": c.get("source")}
        for c in rows
    ]
    focus = []
    if commodity:
        cl = commodity.lower()
        focus = [i for i in items if cl in (i["crop"] or "").lower()]
    return {
        "ok": True, "city": city, "state": state,
        "commodity": commodity,
        "focus": focus[:3],
        "items": items[:8],
    }


def _chat_seasonal_tool(city):
    season = get_season(datetime.now().month)
    advisories = _seasonal_advisories(season, city or "")
    return {
        "ok": True, "season": season, "city": city or "",
        "advisories": [
            {"category": r["category"], "title": r["title"],
             "message": r["message"], "action": r.get("action")}
            for r in advisories
        ],
    }

def _chat_run_gateway(intents, context_data):
    """Execute the requested features and return (sections, actions, summaries).
    sections  — [] str, appended to the LLM's context so the reply uses LIVE data.
    actions   — [] dict {type,label,url}, navigation chips for the frontend.
    summaries — [] str, brief one-line LIVE-data cards appended to the reply so
                the farmer is GUARANTEED to see the fetched numbers even if the
                model's wording omits them."""
    lat = context_data.get("lat")
    lon = context_data.get("lon")
    city = context_data.get("city") or context_data.get("user_city") or ""
    commodity = context_data.get("commodity") or ""
    sections = []
    actions = []
    summaries = []
    seen = set()

    if "weather" in intents:
        res = _chat_weather_tool(lat, lon)
        if res.get("ok"):
            sections.append(
                f"[LIVE WEATHER] City: {res.get('city') or 'your location'} | "
                f"Now: {res.get('temp_c')}°C, {res.get('humidity_pct')}% RH, "
                f"{res.get('description')}, {res.get('wind_mps')} m/s wind, "
                f"{res.get('rain_mm')} mm rain\n"
                f"7-day outlook: {res.get('forecast_text')}"
            )
            summaries.append(
                f"🌤️ {res.get('city')}: {res.get('temp_c')}°C, "
                f"{res.get('humidity_pct')}% humidity, {res.get('description')}."
            )
        else:
            sections.append(
                "[WEATHER] Could not fetch live weather. Ask the farmer for "
                "their location so you can check weather and alerts for them."
            )

    if "alerts" in intents:
        res = _chat_alerts_tool(lat, lon)
        if res.get("ok") and res.get("alerts"):
            lines = "\n".join(
                f"- [{a['category']}] {a['title']}: {a['message']} → {a['action']}"
                for a in res["alerts"]
            )
            sections.append(
                f"[LIVE WEATHER-BASED ALERTS] {res.get('city') or 'your area'}\n{lines}"
            )
            first = res["alerts"][0]
            summaries.append(f"⚠️ {res.get('city')}: {first['title']} — {first['message']}")
        elif res.get("ok"):
            summaries.append(f"✅ {res.get('city')}: no active pest or weather alerts right now.")
        else:
            sections.append(
                "[WEATHER ALERTS] No live alerts were computed right now. You can "
                "still give general advice about pests and diseases."
            )

    if "market" in intents:
        res = _chat_market_tool(city, commodity)
        if res.get("ok") and res.get("items"):
            shown = res.get("focus") or res.get("items", [])[:3]
            rows_str = "\n".join(
                f"  • {i['crop']}: ₹{i['price']}/q ({i['change_pct']}%)"
                for i in shown
            )
            asked = f" — asked about {commodity}" if commodity else ""
            sections.append(
                f"[LIVE MANDI PRICES] {res.get('city')}, {res.get('state')}{asked}\n{rows_str}"
            )
            fallback_items = res.get("focus") if res.get("focus") else res.get("items", [])
            for i in fallback_items[:3]:
                arrow = "▲" if i["change_pct"] >= 0 else "▼"
                summaries.append(f"🏪 {res.get('city')}: {i['crop']} ₹{i['price']}/q {arrow} {i['change_pct']}%")
        elif res.get("reason") == "no_city":
            sections.append(
                "[MARKET PRICES] Ask the farmer which city they want mandi rates "
                "for, then check the live prices for that city."
            )
        else:
            sections.append(
                "[MARKET PRICES] Prices unavailable right now. The data service "
                "(Agmarknet / data.gov.in) may be slow or offline — answer from "
                "your general knowledge and suggest checking the Market tab."
            )

    if "crops" in intents:
        res = _chat_crops_tool(lat, lon, city)
        if res.get("ok") and res.get("crops"):
            crops = "\n".join(
                f"  • {c['name']} — {c.get('match', '')}"
                for c in res["crops"]
            )
            sections.append(
                f"[CROP RECOMMENDATIONS] Season: {res.get('season')}, "
                f"Location: {res.get('city') or 'general'}\n{crops}"
            )
            summaries.append(
                f"🌾 Recommended for {res.get('season')}: "
                + ", ".join(c["name"] for c in res["crops"][:4])
            )
        else:
            sections.append(
                "[CROP RECOMMENDATIONS] Could not compute recommendations right "
                "now — suggest general season-appropriate crops instead."
            )

    if "seasonal" in intents:
        res = _chat_seasonal_tool(city)
        if res.get("ok") and res.get("advisories"):
            adv = "\n".join(
                f"  • [{a['category']}] {a['title']}: {a['message']} → {a['action']}"
                for a in res["advisories"]
            )
            sections.append(
                f"[SEASONAL ADVISORIES] Season: {res.get('season')}, "
                f"Location: {res.get('city') or 'general'}\n{adv}"
            )
            summaries.append(
                f"🗓️ {res.get('season')} advice: {res['advisories'][0]['title']}."
            )
        else:
            sections.append(
                "[SEASONAL ADVISORIES] Could not fetch advisories — answer from "
                "your general knowledge of the current season."
            )

    if "diagnosis" in intents:
        sections.append(
            "[CROP DIAGNOSIS] You cannot view photos in this chat. Tell the "
            "farmer you can start the app's crop-diagnosis tool for them, and "
            "ask them to tap the 📷 Diagnose Crop button (or /diagnose page) and "
            "upload a clear photo of the affected plant/leaf so the AI can "
            "analyze it."
        )

    # Navigation action chips (max 5)
    for feature in ("diagnosis", "alerts", "market", "weather", "crops", "seasonal"):
        if feature in intents and feature not in seen:
            label, url = _FEATURE_LABEL_MAP.get(feature, ("", ""))
            if label and url:
                actions.append({"type": "navigate", "label": label, "url": url})
                seen.add(feature)
                if len(actions) >= 5:
                    break

    return sections, actions, summaries

@app.route("/api/chat", methods=["POST"])
def kisan_chat():
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 500

    ip = request.remote_addr or "unknown"
    if _is_rate_limited(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.json or {}
    messages = data.get("messages", [])
    lang = str(data.get("lang", "en")).strip().lower()
    messages = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not messages:
        return jsonify({"error": "No messages"}), 400

    lang_name = LANG_NAMES.get(lang, "English")

    # ── Topic gate (#chat-limits) ───────────────────────────
    # Kisan Helper ONLY answers agriculture / SmartAgro questions. Before we
    # ever pay for a Groq call, cheaply classify the latest user message:
    #   1. Fast-path: if it contains strong agricultural keywords (in English
    #      or any of India's major languages) it's obviously on-topic → skip
    #      the classifier and proceed. This avoids a network call on every
    #      genuine farmer question.
    #   2. Otherwise run a tiny throwaway classifier pass (temp 0, max_tokens 32)
    #      that returns {"on_topic": true/false}.
    #   3. If anything fails, we FAIL OPEN (treat as on-topic) — a network
    #      hiccup must never block a real farmer's disease/pest question.
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if last_user and not _chat_message_on_topic(last_user):
        return jsonify({"reply": _off_topic_reply(lang), "off_topic": True})

    context_data = data.get("context", {})
    if not context_data.get("city"):
        context_data["city"] = _chat_detect_city(last_user)
    if not context_data.get("commodity"):
        context_data["commodity"] = _chat_detect_commodity(last_user)

    # ── Feature Gateway ───────────────────────────────────────────
    # Detect which SmartAgro features this question maps to and run them
    # NOW, so the reply can be grounded in live data from the real app.
    gate_intents = _chat_detect_intents(last_user)
    gate_sections, gate_actions, gate_summaries = _chat_run_gateway(gate_intents, context_data)

    context_str = ""
    if context_data:
        lat, lon = context_data.get("lat"), context_data.get("lon")
        weather = context_data.get("weather")
        crops = context_data.get("crops")
        
        if lat and lon:
            context_str += f"\n- User Location: approx. {lat}, {lon}"
        if weather:
            context_str += f"\n- Current Weather: {weather.get('temp', '?')}°C, {weather.get('humidity', '?')}% humidity"
            if weather.get('city'):
                context_str += f" in {weather.get('city')}"
        if crops and isinstance(crops, list):
            crop_names = [c.get("name") for c in crops[:3] if isinstance(c, dict)]
            if crop_names:
                context_str += f"\n- Recommended Crops for them: {', '.join(crop_names)}"

    system_prompt = f"""You are Kisan Helper, a highly specialized AI assistant built ONLY for Indian farmers inside the SmartAgro app. You are NOT a general-purpose chatbot.

HARD RULE — TOPIC LIMIT: You ONLY answer questions about farming, agriculture, and the SmartAgro app. Allowed topics: crop diseases & pests, weather/location advice for crops, pesticide/fertilizer/compost usage, market & mandi prices, government farm schemes & subsidies (PM-KISAN, KCC, crop insurance), soil health, irrigation, and sowing/harvest timing.

STRICT REFUSAL: If the user asks about ANYTHING outside farming and the SmartAgro app — general knowledge, news, movies, sports, music, fashion, jokes, coding, math, science, history, recipes, travel, personal or health advice, gossip, or any other unrelated chit-chat — you MUST NOT answer it, even partially, even if the user insists or repeats the request. Instead, politely refuse and redirect them with wording like: "I'm Kisan Helper — I only help with farming and the SmartAgro app. Ask me about crops, pests, soil, weather, market prices, or schemes like PM-KISAN." Reply in {lang_name}.

LANGUAGE: The user may write in any language; always reply ONLY in {lang_name}, in its native script (not transliteration).

STYLE: Keep answers practical, simple, and farmer-friendly. Use bullet points. No markdown headers, no fluff. Under 200 words. If unsure, say so and suggest consulting a local farm expert."""

    if context_str:
        system_prompt += f"\n\nContext about the user's current situation (use this if they ask about weather, location, or what to grow):{context_str}"

    if gate_sections:
        system_prompt += (
            "\n\nFEATURE DATA — the app fetched these exact LIVE numbers right now "
            "from the real sources (same ones the dashboard uses). Your answer to "
            "this question MUST quote these numbers and say which city/place they "
            "are for. Never claim the data is unavailable — it is right here. Copy "
            "the figures verbatim; do not invent or replace them:"
            "\n" + "\n\n".join(gate_sections)
        )

    headers = _ai_headers()
    body = {
        "model":       AI_CHAT_MODEL,
        "messages":    [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.7,
        "max_tokens":  700,
        "stream":      False
    }
    try:
        resp = requests.post(AI_COMPLETIONS_URL, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            return jsonify({"error": "AI unavailable"}), 500
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        if gate_summaries:
            live_block = "📊 Live data:\n" + "\n".join(gate_summaries)
            reply = reply.rstrip() + "\n\n" + live_block
        return jsonify({"reply": reply, "actions": gate_actions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Kisan Helper — Speech-to-Text (Groq Whisper) ────────────────────────────
# Works identically on every OS/browser because the audio is recorded with the
# standard MediaRecorder API (supported on Chrome, Safari/iOS, Firefox, Edge,
# all Android browsers) and transcribed server-side — no dependency on the
# patchy, Chrome-only browser SpeechRecognition API.
STT_LIMIT = 20
MAX_AUDIO_B64_LEN = 8 * 1024 * 1024  # ~6 MB raw audio, generous for a voice note


def _is_rate_limited_stt(ip: str) -> bool:
    return not _rate_limit("stt", ip, STT_LIMIT)


@app.route("/api/stt", methods=["POST"])
def speech_to_text():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY not set in .env"}), 500

    ip = request.remote_addr or "unknown"
    if _is_rate_limited_stt(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    audio_file = request.files.get("audio")
    # NOTE: we deliberately do NOT force a Whisper language hint from the
    # selected reply-language. The farmer may speak in a different language
    # than the one chosen for replies (that's the whole point of "ask in any
    # language, reply in the selected one") — Whisper's own auto-detection
    # handles that mismatch far better than a forced hint would.

    if not audio_file:
        return jsonify({"error": "No audio received"}), 400

    audio_bytes = audio_file.read()
    if len(audio_bytes) > MAX_AUDIO_B64_LEN:
        return jsonify({"error": "Recording too long. Please keep it under ~60 seconds."}), 413
    if len(audio_bytes) < 500:
        return jsonify({"error": "Recording too short or empty. Please try again."}), 400

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {
        "file": (audio_file.filename or "voice.webm", audio_bytes, audio_file.mimetype or "audio/webm"),
    }
    form_data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "json",
        "temperature": 0,
    }

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=form_data, timeout=30
        )
        if resp.status_code != 200:
            logger.warning(f"[STT error] {resp.status_code}: {resp.text[:300]}")
            return jsonify({"error": "Could not transcribe audio"}), 500
        text = resp.json().get("text", "").strip()
        return jsonify({"text": text})
    except Exception as e:
        logger.warning(f"[STT exception] {e}")
        return jsonify({"error": str(e)}), 500


# ─── Diagnose Crop via Groq Vision ───────────────────────────────────────────
MAX_IMAGE_B64_LEN = 2 * 1024 * 1024
DIAGNOSE_LIMIT  = 10

# Vision-capable models tried per ensemble pass. Today Groq only has one
# production-viable multimodal model on the general tier (see the note by
# ENSEMBLE_PASSES above) — meta-llama/llama-4-scout-17b-16e-instruct was
# deprecated June 17, 2026. Add a second entry here as soon as one exists;
# no other code needs to change.
vision_models = [
    "qwen/qwen3.6-27b",
]


def _is_rate_limited_diagnose(ip: str) -> bool:
    return not _rate_limit("diagnose", ip, DIAGNOSE_LIMIT)


# ── Step 0: cheap pre-classifier ─────────────────────────────────────────
def ai_is_crop_image(image_b64):
    """Fast, low-token sanity check BEFORE running the full diagnosis
    prompt: does this photo actually show a plant/crop part? Without this,
    the main prompt will happily hallucinate a plausible-sounding disease
    name for a photo of a hand, a sack of grain, or a selfie — which is
    worse than useless for a farmer trying to protect a crop.
    Fails OPEN (assumes "yes, it's a plant") on any error/timeout, so a
    flaky classifier call never blocks a genuine diagnosis."""
    if not GROQ_API_KEY:
        return True, None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": vision_models[0],
        "messages": [
            {"role": "system", "content": "You classify images. Return ONLY valid JSON, nothing else."},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": (
                    "Is this photo of a plant, crop, leaf, stem, fruit, or root — "
                    "the kind of close-up photo a farmer would take to check a crop for "
                    "disease? Answer false for people, animals, food dishes, documents, "
                    "landscapes with no visible plant detail, or anything unrelated.\n"
                    'Respond ONLY with JSON: {"is_plant": true or false, "reason": "one short phrase"}'
                )}
            ]}
        ],
        "temperature": 0,
        "max_tokens": 100,
        "reasoning_effort": "none",
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=headers, json=body, timeout=15)
        if resp.status_code != 200:
            return True, None  # fail open — don't block a real diagnosis on a classifier hiccup
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        parsed = json.loads(match.group() if match else cleaned)
        return bool(parsed.get("is_plant", True)), parsed.get("reason")
    except Exception as e:
        logger.warning(f"[PreClassifier] error: {e}")
        return True, None  # fail open


# ── Ensemble helpers ──────────────────────────────────────────────────────
def _run_vision_pass(image_b64, prompt, sys_prompt, model, temperature):
    """Run one diagnosis pass against one model and return the parsed JSON,
    or None if that pass failed for any reason (caller decides how many
    successful passes it needs)."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": prompt}
            ]}
        ],
        "temperature": temperature,
        "max_tokens": 1400,
        "reasoning_effort": "none",  # skip <think> mode so JSON lands directly in content
    }
    resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers=headers, json=body, timeout=45)
    if resp.status_code != 200:
        return None
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group())


def _run_gemini_pass(image_b64, prompt, sys_prompt):
    """Run one diagnosis pass against Google's Gemini API and return the
    parsed JSON, or None on any failure. When GEMINI_API_KEY is configured
    this gives the ensemble a genuinely INDEPENDENT second model (different
    vendor, different weights) instead of re-running the same Groq vision
    model at a second temperature — so an agreement between Groq & Gemini
    is real cross-model evidence, which is exactly what the QA log wants."""
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_DIAGNOSIS_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1200},
    }
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=45)
        if resp.status_code != 200:
            logger.warning(f"[Diagnose] Gemini HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        return json.loads(match.group())
    except Exception as e:
        logger.warning(f"[Diagnose] Gemini exception: {e}")
        return None


def _diseases_agree(name_a, name_b):
    """Fuzzy-match two disease name strings so small phrasing differences
    between passes ('Late Blight' vs 'Late blight disease') still count as
    agreement, while genuinely different diagnoses are correctly flagged as
    a disagreement."""
    if not name_a or not name_b:
        return False
    a, b = name_a.strip().lower(), name_b.strip().lower()
    if a == b:
        return True
    if "healthy" in a and "healthy" in b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


# ── QA logging: store every image + result for human spot-checking ──────
def _save_diagnosis_image(image_b64, record_id):
    """Persist the uploaded image next to its diagnosis record so a
    reviewer can see exactly what the model saw. Returns the saved
    filename, or None on failure (non-fatal — logging never blocks the
    response the farmer is waiting on)."""
    try:
        raw = base64.b64decode(image_b64)
        img_hash = hashlib.sha256(raw).hexdigest()[:12]
        filename = f"{record_id}_{img_hash}.jpg"
        with open(os.path.join(DIAGNOSIS_IMAGES_DIR, filename), "wb") as f:
            f.write(raw)
        return filename
    except Exception as e:
        logger.warning(f"[DiagnosisLog] Could not save image: {e}")
        return None


def _log_diagnosis(record):
    """Append one diagnosis record (image ref + every ensemble pass' raw
    output + the merged final answer) to a JSONL audit log. This is the
    data a human reviewer or a future accuracy benchmark would need —
    without it there is no way to check the model against reality."""
    try:
        with _diagnosis_log_lock:
            with open(DIAGNOSIS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[DiagnosisLog] Could not write log entry: {e}")


@app.route("/api/diagnose", methods=["POST"])
def diagnose_crop():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY not set in .env"}), 500

    ip = request.remote_addr or "unknown"
    if _is_rate_limited_diagnose(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.json or {}
    image_b64 = data.get("image", "")
    lang      = str(data.get("lang", "en")).strip().lower()

    if not image_b64:
        return jsonify({"error": "No image data received"}), 400
    if len(image_b64) > MAX_IMAGE_B64_LEN:
        return jsonify({"error": "Image too large. Please use an image under 1 MB."}), 413

    # ── Step 1: decode + validate the image ──────────────────────────────
    # NOTE: intentionally NO dedup / short-circuit here. A farmer re-submitting
    # the same photo (UI retry, double-tap, retrying from a flaky network)
    # always re-runs the full pipeline so they get a fresh, live diagnosis
    # instead of a stale replay. The image SHA-256 below is kept only as an
    # audit identifier for the QA log / saved image filename — it never skips
    # a real diagnosis.
    try:
        image_raw = base64.b64decode(image_b64)
    except Exception:
        return jsonify({"error": "Image data is not valid base64"}), 400
    image_sha256 = hashlib.sha256(image_raw).hexdigest()

    # ── Step 2: cheap pre-classifier — reject non-crop photos early ─────
    is_plant, reject_reason = ai_is_crop_image(image_b64)
    if not is_plant:
        return jsonify({
            "error": "not_a_plant",
            "message": "This doesn't look like a photo of a plant, leaf, stem, fruit, or root.",
            "detail": reject_reason,
        }), 422

    lang_name = LANG_NAMES.get(lang, "")
    if lang != "en" and lang_name:
        lang_instruction = (
            f"\n\nIMPORTANT: Write ALL text values in {lang_name} "
            f"(except JSON keys, numbers, chemical/brand names, units such as "
            f"kg/ha, ml/L, g/ha, %, SL, EC, SC, WP, SG, NPK, and dose figures — "
            f"keep those in English/digits as-is)."
        )
    else:
        lang_instruction = ""

    prompt = f"""You are an expert agricultural plant pathologist AI. Look very carefully at this crop image.
Respond ONLY with valid JSON, no markdown or backticks:
{{
  "disease": "Exact disease name",
  "confidence": 88,
  "severity": "Mild or Moderate or Severe",
  "affected_part": "Leaves/Stem/Fruit/Root/Cob",
  "cause": "Specific pathogen and spread method",
  "eco_remedies": [{{"remedy": "Remedy", "method": "Steps", "frequency": "How often", "effectiveness": 80}}],
  "chemical_remedies": [{{"name": "Chemical", "dose": "Dose per litre", "interval": "Days between sprays"}}],
  "prevention": ["tip1", "tip2", "tip3"],
  "recovery_timeline": "Weeks for recovery"
}}{lang_instruction}"""

    sys_prompt = "Expert plant pathologist. Return ONLY valid JSON."
    if lang != "en" and lang_name:
        sys_prompt += f" All free-text values must be in {lang_name}."

    # ── Step 3: ensemble / self-consistency passes ──────────────────────
    # Cycling through `vision_models` means this automatically becomes a
    # true multi-model ensemble the moment a second entry is added there;
    # with a single model configured (today's reality) it instead runs
    # that model twice at different temperatures as a self-consistency
    # cross-check, which is the honest equivalent given only one
    # production-viable Groq vision model currently exists.
    #
    # The passes are independent (same image, different temperature/model),
    # so they're fired off in parallel via ThreadPoolExecutor rather than
    # awaited one-by-one — this is a pure I/O-bound wait on the Groq API,
    # so running them concurrently cuts a farmer's wait roughly in half
    # instead of paying for each pass's latency back-to-back.
    pass_temperatures = [0.2, 0.6, 0.9]
    pass_plan = [
        (i, vision_models[i % len(vision_models)], pass_temperatures[i % len(pass_temperatures)])
        for i in range(ENSEMBLE_PASSES)
    ]
    # When a Gemini key is configured, append a genuinely INDEPENDENT second
    # model to the ensemble. The "gemini" token here is a dispatch marker;
    # the real model id is recorded in `models_used` so the QA log and the
    # review tool show exactly which models actually ran.
    if GEMINI_API_KEY:
        pass_plan.append((len(pass_plan), "gemini", 0.3))
    pass_outcomes = [None] * len(pass_plan)  # preserve original pass order in results

    def _run_pass(i, model, temp):
        try:
            if model == "gemini":
                return _run_gemini_pass(image_b64, prompt, sys_prompt)
            return _run_vision_pass(image_b64, prompt, sys_prompt, model, temp)
        except Exception as e:
            logger.warning(f"[Diagnose] pass {i} ({model}) failed: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pass_plan)) as executor:
        future_to_pass = {
            executor.submit(_run_pass, i, model, temp): (i, model)
            for i, model, temp in pass_plan
        }
        for future in concurrent.futures.as_completed(future_to_pass):
            i, model = future_to_pass[future]
            parsed = future.result()
            if parsed and parsed.get("disease"):
                pass_outcomes[i] = (parsed, model)

    results, models_used = [], []
    gemini_display = f"gemini:{GEMINI_DIAGNOSIS_MODEL}"
    for outcome in pass_outcomes:
        if outcome is not None:
            parsed, model = outcome
            results.append(parsed)
            models_used.append(gemini_display if model == "gemini" else model)

    if not results:
        return jsonify({"error": "All vision models failed. Check your GROQ_API_KEY in .env"}), 500

    # ── Step 4: merge / vote across passes ───────────────────────────────
    primary = max(results, key=lambda r: r.get("confidence", 0))
    others  = [r for r in results if r is not primary]

    agreement = True
    alternate_diagnosis = None
    if others:
        agree_flags = [_diseases_agree(primary.get("disease", ""), o.get("disease", "")) for o in others]
        agreement = all(agree_flags)
        if agreement:
            # Independent passes landed on the same diagnosis — that
            # agreement is itself evidence, so average + slightly boost
            # confidence (capped at 99, never claim certainty).
            confidences = [r.get("confidence", 0) for r in results]
            primary["confidence"] = min(99, round(sum(confidences) / len(confidences)) + 5)
        else:
            # Passes disagree — keep the higher-confidence answer but
            # discount it, and surface the alternate so the farmer (and
            # any human reviewer reading the log) can see the model wasn't
            # actually sure, instead of a falsely-confident single answer.
            primary["confidence"] = max(30, round(primary.get("confidence", 50) * 0.7))
            disagreeing = next((o for o, f in zip(others, agree_flags) if not f), None)
            if disagreeing:
                alternate_diagnosis = disagreeing.get("disease")

    primary["_lang"] = lang
    primary["model_agreement"] = agreement
    primary["_passes_run"] = len(results)
    primary["_models_used"] = models_used   # e.g. ["qwen/qwen3.6-27b", "gemini:gemini-3.1-flash-lite"]
    if alternate_diagnosis:
        primary["alternate_diagnosis"] = alternate_diagnosis

    # ── Step 5: log image + full result for human spot-checking ─────────
    # The audit entry stores the image's full SHA-256, the exact response
    # payload, and every pass's raw output, so a human reviewer can compare
    # the model's answer against the real photo. Each re-submission of the
    # same image creates a fresh entry (no dedup) — re-runs always reflect
    # the live model, never a stale replay.
    record_id = f"{int(time.time()*1000)}_{ip.replace('.', '-').replace(':', '-')}"
    image_filename = _save_diagnosis_image(image_b64, record_id)
    _log_diagnosis({
        "id":                   record_id,
        "timestamp":            datetime.now().isoformat(),
        "ip":                   ip,
        "lang":                 lang,
        "image_sha256":         image_sha256,
        "models_used":          models_used,
        "passes_run":           len(results),
        "model_agreement":      agreement,
        "final_disease":        primary.get("disease"),
        "final_confidence":     primary.get("confidence"),
        "alternate_diagnosis":  alternate_diagnosis,
        "severity":             primary.get("severity"),
        "image_file":           image_filename,
        "raw_results":          results,   # every pass' full untouched output
        "response":             primary,   # exact JSON the API returned (audit/QA replay)
        "human_reviewed":       False,     # a reviewer can flip this after checking the image
        "human_verdict":        None,      # "correct" | "incorrect" | "uncertain"
    })

    return jsonify(primary)


# ─── Diagnosis QA review endpoints (internal, DEBUG_MODE only) ──────────────
@app.route('/api/diagnose-log')
def diagnose_log():
    """Lets a human reviewer list recent diagnoses to spot-check the AI
    against the real uploaded photo. This is intentionally an internal QA
    tool, not a farmer-facing feature — it exposes raw model output and
    request IPs, so it's gated behind FLASK_DEBUG=1."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    limit = min(int(request.args.get('limit', 50)), 500)
    entries = []
    try:
        with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    return jsonify({"count": len(entries), "entries": entries})


@app.route('/api/diagnose-log/image/<path:filename>')
def diagnose_log_image(filename):
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    safe_name = os.path.basename(filename)  # prevent path traversal
    path = os.path.join(DIAGNOSIS_IMAGES_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    from flask import send_file
    return send_file(path, mimetype="image/jpeg")


@app.route('/api/diagnose-log/review', methods=["POST"])
def diagnose_log_review():
    """Lets a reviewer record a verdict ("correct"/"incorrect"/"uncertain")
    against a logged diagnosis by rewriting its line in the JSONL file.
    This closes the loop: stored images + reviewer verdicts are exactly
    the labeled data needed to eventually compute a real accuracy number
    instead of relying on the model's own self-reported confidence."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    data = request.json or {}
    record_id = data.get("id")
    verdict = data.get("verdict")
    if not record_id or verdict not in ("correct", "incorrect", "uncertain"):
        return jsonify({"error": "Provide id and verdict (correct|incorrect|uncertain)"}), 400

    try:
        with _diagnosis_log_lock:
            with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            updated = False
            for i, line in enumerate(lines):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == record_id:
                    rec["human_reviewed"] = True
                    rec["human_verdict"] = verdict
                    lines[i] = json.dumps(rec, ensure_ascii=False) + "\n"
                    updated = True
                    break
            if updated:
                # Rewrite ATOMICALLY so a crash mid-rewrite can't truncate or
                # corrupt the audit log. The whole read-modify-write is already
                # under _diagnosis_log_lock.
                with open(DIAGNOSIS_LOG_PATH + ".tmp", "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(DIAGNOSIS_LOG_PATH + ".tmp", DIAGNOSIS_LOG_PATH)
        return jsonify({"status": "ok" if updated else "not_found"}), (200 if updated else 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/diagnose-log/accuracy')
def diagnose_log_accuracy():
    """Computes real accuracy from whatever human verdicts have been
    recorded so far. Returns 0 reviewed entries until someone actually
    uses /api/diagnose-log/review — which is the honest state until real
    review happens, rather than a fabricated number."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    total = reviewed = correct = 0
    agreement_correct = agreement_total = 0
    try:
        with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("cached"):
                    continue   # dedup replay entries aren't independent diagnoses
                total += 1
                if rec.get("human_reviewed"):
                    reviewed += 1
                    is_correct = rec.get("human_verdict") == "correct"
                    if is_correct:
                        correct += 1
                    if rec.get("model_agreement"):
                        agreement_total += 1
                        if is_correct:
                            agreement_correct += 1
    except FileNotFoundError:
        pass

    return jsonify({
        "total_logged":            total,
        "human_reviewed":          reviewed,
        "accuracy_reviewed_only":  round(correct / reviewed, 3) if reviewed else None,
        "agreement_case_accuracy": round(agreement_correct / agreement_total, 3) if agreement_total else None,
        "note": "accuracy_reviewed_only is ONLY meaningful once a human has reviewed "
                "a reasonably-sized, representative sample via /api/diagnose-log/review. "
                "agreement_case_accuracy shows whether the model_agreement flag actually "
                "predicts correctness — the thing to check before trusting it as a signal.",
    })


# ─── Alerts ──────────────────────────────────────────────────────────────────
@app.route("/api/alerts", methods=["POST"])
def get_alerts():
    data        = request.json or {}
    temp        = data.get("temp", 25)
    humidity    = data.get("humidity", 60)
    wind_speed  = data.get("wind_speed", 10)
    rain        = data.get("rain", 0)
    description = data.get("description", "").lower()
    alerts      = []

    if temp > 40:
        alerts.append({"type":"danger","category":"Weather","icon":"🌡️","title":"Extreme Heat Alert","message":"Temperature above 40°C. Provide shade netting and increase irrigation frequency.","action":"Schedule irrigation every 4-5 hours. Avoid afternoon spraying."})
    if temp < 5:
        alerts.append({"type":"danger","category":"Weather","icon":"❄️","title":"Frost Warning","message":"Sub-zero temperatures expected. Frost can destroy standing crops overnight.","action":"Cover crops with frost cloth. Use smudge pots or sprinkler irrigation."})
    if humidity > 85:
        alerts.append({"type":"warning","category":"Disease","icon":"🍄","title":"High Fungal Disease Risk","message":"Humidity above 85% creates ideal conditions for fungal diseases.","action":"Apply preventive fungicide (Mancozeb 75 WP at 2.5 g/L) immediately."})
    if wind_speed > 50:
        alerts.append({"type":"danger","category":"Weather","icon":"💨","title":"High Wind Speed Alert","message":"Strong winds can cause lodging in tall crops like maize and wheat.","action":"Avoid spraying. Support tall crops with stakes."})
    if rain > 50:
        alerts.append({"type":"warning","category":"Weather","icon":"🌧️","title":"Heavy Rainfall Alert","message":"Excessive rain may cause waterlogging and root rot.","action":"Ensure field drainage channels are open. Pause irrigation."})
    if "storm" in description or "thunder" in description:
        alerts.append({"type":"danger","category":"Weather","icon":"⛈️","title":"Thunderstorm Warning","message":"Thunderstorm conditions detected. Risk of lightning and hail damage.","action":"Stay indoors. Secure farm equipment."})
    if 25 <= temp <= 35 and humidity > 70:
        alerts.append({"type":"warning","category":"Pest","icon":"🐛","title":"Aphid & Whitefly Risk","message":"Warm humid conditions are ideal for aphid multiplication.","action":"Spray Neem oil (5 ml/L) or Imidacloprid 0.3 ml/L at dusk."})
    if temp > 30 and humidity < 50:
        alerts.append({"type":"warning","category":"Pest","icon":"🕷️","title":"Spider Mite Alert","message":"Hot dry conditions favour rapid spider mite population growth.","action":"Apply Abamectin 1.8 EC (0.5 ml/L). Increase soil moisture."})

    harmful = []
    if temp > 38:                   harmful.append("Wheat (grain shriveling risk)")
    if humidity > 85 and rain > 20: harmful.append("Cotton (boll rot risk)")
    if temp < 10:                   harmful.append("Rice (cold injury risk)")
    if harmful:
        alerts.append({"type":"info","category":"Crop Advisory","icon":"🌾","title":"Crops at Risk in Current Conditions","message":f"Avoid growing: {', '.join(harmful)}","action":"Consider alternate crops better suited to current climate."})

    return jsonify({"alerts": alerts, "total": len(alerts)})

# ─── Multi-period Alert Tiers (#2) ───────────────────────────────────────────
# /api/alerts above only covers the CURRENT instant. These endpoints extend
# it to the 6-day forecast, a 30-day monthly outlook, city+season advisories
# and a per-crop risk % with a best-harvest window. They reuse the exact same
# rule engine as /api/alerts (as a pure function) so every tier stays
# consistent with the "now" view.

def _rule_alerts(temp, humidity, wind_speed, rain, description=""):
    """The /api/alerts rule engine as a pure per-day function, so the
    forecast / monthly / seasonal tiers can evaluate any future day
    identically to the current conditions a farmer sees today."""
    description = (description or "").lower()
    alerts = []
    if temp > 40:
        alerts.append({"type":"danger","category":"Weather","icon":"🌡️","title":"Extreme Heat Alert","message":"Temperature above 40°C. Provide shade netting and increase irrigation frequency.","action":"Schedule irrigation more often. Avoid afternoon spraying."})
    if temp < 5:
        alerts.append({"type":"danger","category":"Weather","icon":"❄️","title":"Frost Warning","message":"Sub-zero temperatures could damage standing crops overnight.","action":"Cover crops with frost cloth. Use sprinkler irrigation if available."})
    if humidity > 85:
        alerts.append({"type":"warning","category":"Disease","icon":"🍄","title":"High Fungal Disease Risk","message":"Humidity above 85% creates ideal conditions for fungal diseases.","action":"Apply preventive fungicide (Mancozeb 75 WP at 2.5 g/L)."})
    if wind_speed > 50:
        alerts.append({"type":"danger","category":"Weather","icon":"💨","title":"High Wind Speed Alert","message":"Strong winds can cause lodging in tall crops like maize and wheat.","action":"Avoid spraying. Support tall crops with stakes."})
    if rain > 50:
        alerts.append({"type":"warning","category":"Weather","icon":"🌧️","title":"Heavy Rainfall Alert","message":"Excessive rain may cause waterlogging and root rot.","action":"Open field drainage channels. Pause irrigation."})
    if "storm" in description or "thunder" in description:
        alerts.append({"type":"danger","category":"Weather","icon":"⛈️","title":"Thunderstorm Warning","message":"Thunderstorm conditions detected. Risk of lightning and hail.","action":"Stay indoors. Secure farm equipment."})
    if 25 <= temp <= 35 and humidity > 70:
        alerts.append({"type":"warning","category":"Pest","icon":"🐛","title":"Aphid & Whitefly Risk","message":"Warm humid conditions favour aphid multiplication.","action":"Spray Neem oil (5 ml/L) or Imidacloprid 0.3 ml/L at dusk."})
    if temp > 30 and humidity < 50:
        alerts.append({"type":"warning","category":"Pest","icon":"🕷️","title":"Spider Mite Alert","message":"Hot dry conditions favour rapid spider mite growth.","action":"Apply Abamectin 1.8 EC (0.5 ml/L). Increase soil moisture."})
    harmful = []
    if temp > 38:   harmful.append("Wheat (grain shriveling risk)")
    if humidity > 85 and rain > 20: harmful.append("Cotton (boll rot risk)")
    if temp < 10:   harmful.append("Rice (cold injury risk)")
    if harmful:
        alerts.append({"type":"info","category":"Crop Advisory","icon":"🌾","title":"Crops at Risk","message":"Avoid growing: " + ", ".join(harmful),"action":"Consider more climate-appropriate crops."})
    return alerts

@app.route("/api/alerts-forecast", methods=["POST"])
def alerts_forecast():
    """Day-by-day forecast alerts + a per-day risk score (0-100) over the
    next week, reusing the same rule engine as /api/alerts."""
    data = request.json or {}
    forecast = data.get("forecast") or []
    items = []
    for day in forecast[:7]:
        t = float(day.get("temp_max", 25))
        h = float(day.get("humidity", 60))
        w = float(day.get("wind_speed", 10))
        r = float(day.get("rain", 0))
        als = _rule_alerts(t, h, w, r, day.get("description", ""))
        risk = min(100, len(als) * 12 + (10 if t > 40 else 0) + (8 if h > 85 else 0))
        items.append({
            "date":        day.get("date"),
            "temp_max":    round(t, 1),
            "temp_min":    round(float(day.get("temp_min", t)), 1),
            "humidity":    round(h, 1),
            "rain":        round(r, 1),
            "description": day.get("description", ""),
            "alerts":      als,
            "risk":        risk,
        })
    if not items:
        return jsonify({"forecast": [], "risk_level": "unknown"})
    risks = [i["risk"] for i in items]
    avg = sum(risks) / len(risks)
    risk_level = "high" if avg >= 50 else ("moderate" if avg >= 25 else "low")
    worst = max(items, key=lambda x: x["risk"])
    best = min(items, key=lambda x: x["risk"])
    return jsonify({
        "forecast":   items,
        "risk_level": risk_level,
        "avg_risk":   round(avg, 1),
        "worst_day":  worst["date"],
        "worst_risk": worst["risk"],
        "best_day":   best["date"],
    })

def _monthly_advisory_text(season, week):
    """Short season-aware weekly advisory for the 30-day outlook."""
    if "Kharif" in season:
        plan = {1: "Prepare fields & monitor early sowing of monsoon crops.",
                2: "Watch for early fungal outbreaks; keep drainage open.",
                3: "Top-dress nitrogen; scout for stem borer / fall armyworm.",
                4: "Prepare for harvest; reduce irrigation if rain persists."}
    elif "Zaid" in season:
        plan = {1: "Plan short-duration summer crops; maximise irrigation.",
                2: "Mulch to hold soil moisture through dry heat.",
                3: "Watch mites & thrips in hot dry conditions.",
                4: "Harvest early-maturing vegetables before peak heat."}
    else:
        plan = {1: "Sow Rabi wheat/mustard; ensure good field moisture.",
                2: "Continue irrigation; watch aphids and yellow rust.",
                3: "Apply second top-dressing; monitor frost risk.",
                4: "Stop irrigation before harvest; prepare for threshing."}
    return plan.get(week, plan.get(1))


@app.route("/api/monthly-alerts", methods=["POST"])
def monthly_alerts():
    """30-day long-term outlook split into 4 weekly buckets."""
    data = request.json or {}
    season = data.get("season") or get_season(datetime.now().month)
    forecast = data.get("forecast") or []
    weekly = []
    for w in range(1, 5):
        slice_days = forecast[(w - 1) * 7:(w - 1) * 7 + 7]
        if slice_days:
            t = sum(float(d.get("temp_max", 25)) for d in slice_days) / len(slice_days)
            h = sum(float(d.get("humidity", 60)) for d in slice_days) / len(slice_days)
            rain = sum(float(d.get("rain", 0)) for d in slice_days)
        else:  # beyond the 7-day window — season-typical projection
            t, h, rain = ((30.0, 78.0, 18.0) if "Kharif" in season
                          else (34.0, 50.0, 3.0) if "Zaid" in season
                          else (27.0, 62.0, 8.0))
        weekly.append({
            "week":          w,
            "label":         f"Week {w}",
            "temp":          round(t, 1),
            "humidity":      round(h, 1),
            "rain_estimate": round(rain, 1),
            "alerts":        _rule_alerts(t, h, 10, rain, ""),
            "advisory":      _monthly_advisory_text(season, w),
        })
    return jsonify({"season": season, "weeks": weekly})

def _seasonal_advisories(season, city):
    """Season-aware farming advisory cards suitable for the given city."""
    if "Kharif" in season:
        recs = [
            {"category": "Crop Advisory", "icon": "🌱", "title": "Sowing Window",
             "message": f"Monsoon (Jun-Sep) is prime for paddy, maize, cotton & soybean in {city or 'your area'}.",
             "action": "Sow after the first substantial rains; use water-tolerant varieties."},
            {"category": "Irrigation", "icon": "💧", "title": "Avoid Waterlogging",
             "message": "Excess monsoon rain risks root rot in sensitive crops.",
             "action": "Keep field channels clear; stop irrigation when rain is heavy."},
            {"category": "Pest", "icon": "🐛", "title": "Scout New Growth",
             "message": "Humid conditions invite stem borer, fall armyworm & leaf folder.",
             "action": "Walk fields weekly; act at the first sign of damage."},
            {"category": "Disease", "icon": "🍄", "title": "Fungal Watch",
             "message": "High humidity favours blast, blight & leaf spot.",
             "action": "Apply preventive fungicide during cloudy spells."},
        ]
    elif "Zaid" in season:
        recs = [
            {"category": "Crop Advisory", "icon": "🌿", "title": "Short-Duration Crops",
             "message": "Summer (Mar-May) suits short-duration vegetables, chilies & melons.",
             "action": "Choose heat-tolerant, early-maturing varieties."},
            {"category": "Irrigation", "icon": "💧", "title": "Maintain Soil Moisture",
             "message": "Dry heat stresses crops quickly.",
             "action": "Irrigate early morning/evening; mulch to retain moisture."},
            {"category": "Pest", "icon": "🕷️", "title": "Heat-Pest Watch",
             "message": "Hot dry spells trigger spider mites & thrips.",
             "action": "Increase soil moisture; spray safe acaricides if colonies appear."},
        ]
    else:
        recs = [
            {"category": "Crop Advisory", "icon": "🌾", "title": "Rabi Sowing",
             "message": "Winter (Oct-Feb) suits wheat, mustard, chickpea & potato in most belts.",
             "action": "Sow at the right depth; keep field moisture uniform."},
            {"category": "Irrigation", "icon": "💧", "title": "Cold-Wave Protection",
             "message": "Light frost can stress young Rabi crops.",
             "action": "Protect tender fields on cold nights; irrigate lightly before frost."},
            {"category": "Pest", "icon": "🐛", "title": "Aphid & Rust Watch",
             "message": "Cool moist weather favours aphids & yellow rust.",
             "action": "Scout regularly; apply fungicide at the first sign of rust."},
            {"category": "Harvest", "icon": "🌾", "title": "Harvest Planning",
             "message": "Plan harvest as crops mature to catch the best prices.",
             "action": "Check maturity; stop irrigation 10-14 days before harvest."},
        ]
    return recs


@app.route("/api/seasonal-alerts", methods=["POST"])
def seasonal_alerts():
    """Seasonal farming advisories by city."""
    data = request.json or {}
    city = data.get("city", "")
    season = data.get("season") or get_season(datetime.now().month)
    return jsonify({"city": city, "season": season, "advisories": _seasonal_advisories(season, city)})


@app.route("/api/crop-risk", methods=["POST"])
def crop_risk():
    """Per-crop weather risk % over the 6-day forecast + best harvest window,
    using each crop's real temperature band from the recommendation table."""
    data = request.json or {}
    forecast = data.get("forecast") or []
    crops = data.get("crops") or []
    season = data.get("season") or get_season(datetime.now().month)
    band = {c["name"]: c for c in recommend_crops(25, 60, 5, season)}
    default = (18, 32)
    results = []
    for c in crops[:12]:
        name = (c.get("name") if isinstance(c, dict) else c) or ""
        ref = band.get(name, {})
        tmin, tmax = ref.get("temp_range") or default
        days = []
        for day in forecast[:6]:
            t = float(day.get("temp_max", 25))
            h = float(day.get("humidity", 60))
            r = float(day.get("rain", 0))
            score = 100
            if t < tmin: score -= int((tmin - t) * 4)
            if t > tmax: score -= int((t - tmax) * 4)
            if h > 85:   score -= 12
            if r > 40:   score -= 10
            days.append(max(0, min(100, score)))
        score_avg = round(sum(days) / len(days), 1) if days else None
        # `days` are suitability scores (higher = safer); expose genuine RISK %
        # (higher = riskier) so the UI reads intuitively.
        risk = round(100 - (score_avg or 0), 1)
        level = ("high" if risk >= 55 else ("moderate" if risk >= 30 else "low"))
        best = None
        for day in forecast[:6]:
            t = float(day.get("temp_max", 25))
            tm = float(day.get("temp_min", t))
            if (tmin - 2) <= tm and t <= (tmax + 2):
                best = day.get("date")
                break
        results.append({"name": name, "risk": risk, "score": score_avg, "level": level, "best_window": best, "days": days})
    results.sort(key=lambda x: x["risk"] or 0)   # safest first
    return jsonify({"season": season, "crops": results})

# Text AI now talks to Gemini's OpenAI-compatible endpoint (free tier);
# there is no separate Groq chat URL for text anymore — Whisper STT has its own.
TRANSLATE_MODELS = [
    AI_CHAT_MODEL,
]
TRANSLATE_CHUNK_SIZE = 40   
TRANSLATE_MAX_WORKERS = 4  
TRANSLATE_STAGGER_SEC = 0.15 
MIN_CALL_INTERVAL_SEC = 0.2

_model_last_call = {}
_model_throttle_lock = threading.Lock()


def _throttle_model(model):
    """Make sure consecutive calls to the same Groq model are spaced out,
    even across concurrent threads, so a burst of chunk requests doesn't
    look like a rate-limit-violating spike to Groq."""
    with _model_throttle_lock:
        now = time.monotonic()
        next_slot = max(now, _model_last_call.get(model, 0) + MIN_CALL_INTERVAL_SEC)
        _model_last_call[model] = next_slot
        wait = next_slot - now
    if wait > 0:
        time.sleep(wait)


def _post_to_ai(body, headers, max_retries=2):
    """POST to the AI provider (Gemini OpenAI-compatible endpoint) with
    throttling + exponential backoff specifically for HTTP 429 (rate limit).
    Returns the final requests.Response."""
    model = body.get("model")
    resp = None
    for attempt in range(max_retries + 1):
        _throttle_model(model)
        resp = requests.post(AI_COMPLETIONS_URL, headers=headers, json=body, timeout=45)
        if resp.status_code != 429:
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else (1.5 * (attempt + 1))
        except (TypeError, ValueError):
            wait = 1.5 * (attempt + 1)
        if attempt < max_retries:
            time.sleep(min(wait, 6))
    return resp


def _extract_json_object(raw_text):
    """Pull a usable {term: translation} dict out of model output, repairing
    the truncation/formatting issues that show up almost exclusively with
    high-token-cost scripts."""
    text = re.sub(r"```(?:json)?", "", raw_text).replace("```", "").strip()
    text = (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))

    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group() if match else text

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    pairs = re.findall(r'"((?:[^"\\]|\\.)+?)"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if pairs:
        return {k: v for k, v in pairs}
    return None


def _build_translate_prompt(terms_chunk, lang_name, domain_note):
    terms_json = json.dumps(terms_chunk, ensure_ascii=False)
    return f"""You are an expert translator for Indian regional languages. Translate each English term below to {lang_name}.

CRITICAL RULES:
1. Return ONLY a raw JSON object mapping each input term to its {lang_name} translation. No markdown, no backticks, no explanation.
2. Every single key from the input list MUST appear in the output JSON, exactly as written.
3. Keep unchanged: chemical/brand names, numbers, and units (kg/ha, Rs, days, ml/L, g/ha, quintal, SL, EC, SC, WP, SG, NPK).
4. {domain_note}
5. Use the natural everyday word a {lang_name}-speaking farmer would use, not a literal/academic translation.
6. Write in the correct native script of {lang_name}. If a term genuinely has no equivalent, keep the English word as-is rather than leaving it blank.

Input terms (translate ALL of these):
{terms_json}

Output: a single JSON object only."""


def _translate_terms_chunk(terms_chunk, lang_name, domain_note):
    prompt = _build_translate_prompt(terms_chunk, lang_name, domain_note)
    headers = _ai_headers()
    max_tokens = min(4096, 300 + len(terms_chunk) * 150)

    last_error = None
    for model in TRANSLATE_MODELS:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": f"You are an expert Indian regional language translator. You MUST respond with valid JSON only, no other text. Translate everything to {lang_name} using its correct native script."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = _post_to_ai(body, headers)
            if resp.status_code == 400:
                body.pop("response_format", None)
                resp = _post_to_ai(body, headers)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                continue

            raw = resp.json()["choices"][0]["message"]["content"].strip()
            translations = _extract_json_object(raw)
            if not translations:
                last_error = "No JSON found/parsable in response"
                continue

            for term in terms_chunk:
                if term not in translations or not translations[term]:
                    translations[term] = term
            return translations

        except Exception as e:
            last_error = str(e)
            continue

    logger.info(f"[Translate] chunk of {len(terms_chunk)} terms to {lang_name} failed on all models: {last_error}")
    return {term: term for term in terms_chunk}


def _translate_terms(terms, lang_name, domain_note, cache_key, cache_dict):
    """Translate a full term list via small, gently-paced parallel chunks,
    with caching and a cleanup retry pass for chunks that failed outright."""
    if not GROQ_API_KEY:
        return {term: term for term in terms}, False

    if cache_key in cache_dict:
        return cache_dict[cache_key], True

    chunks = [terms[i:i + TRANSLATE_CHUNK_SIZE] for i in range(0, len(terms), TRANSLATE_CHUNK_SIZE)]
    results = [None] * len(chunks)

    def run_chunk(idx):
        results[idx] = _translate_terms_chunk(chunks[idx], lang_name, domain_note)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(TRANSLATE_MAX_WORKERS, len(chunks))) as executor:
        futures = []
        for i in range(len(chunks)):
            if i > 0:
                time.sleep(TRANSLATE_STAGGER_SEC)  # avoid firing every chunk in the same instant
            futures.append(executor.submit(run_chunk, i))
        concurrent.futures.wait(futures)
    for i, chunk in enumerate(chunks):
        if any(results[i].get(term) == term for term in chunk):
            time.sleep(1.5)
            retried = _translate_terms_chunk(chunk, lang_name, domain_note)
            if any(retried.get(term) != term for term in chunk):
                results[i] = retried

    translations = {}
    for r in results:
        translations.update(r)

    cache_dict[cache_key] = translations
    return translations, False


TRANSLATE_LIMIT = 20

def _is_rate_limited_translate(ip: str) -> bool:
    return not _rate_limit("translate", ip, TRANSLATE_LIMIT)


# ─── Market Translation ───────────────────────────────────────────────────────

@app.route("/api/translate-market", methods=["POST"])
def translate_market():
    if _is_rate_limited_translate(request.remote_addr or "unknown"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.json or {}
    lang = str(data.get("lang", "en")).strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    terms = [
        "Wheat","Rice","Paddy (Rice)","Maize (Corn)","Mustard","Groundnut",
        "Onion","Potato","Tomato","Chilli","Sugarcane","Arhar (Tur)","Moong",
        "Urad","Soybean","Soybean Oil","Soybean Meal","Cotton","Jowar (Sorghum)",
        "Bajra (Pearl Millet)","Bengal Gram (Chana)","Garlic","Ginger","Turmeric",
        "Cumin (Jeera)","Coriander","Sunflower","Sesame (Til)","Linseed","Castor Seed",
        "Banana","Mango","Apple","Grapes","Pomegranate","Cabbage","Cauliflower",
        "Brinjal (Eggplant)","Ladyfinger (Okra)","Spinach","Bitter Gourd","Bottle Gourd",
        "Ridge Gourd","Ash Gourd","Palm Oil","Oats","Coffee","Cocoa","Rubber","Lumber",
        "Very High","High","Medium","Low","Price Rising","Price Falling",
        "Very High Demand","All","Crop","Price","Change","Demand",
        "Trend","Comparison","Demand Map","Search","30-Day Price Trend",
        "Current Prices","Demand Intensity","Price Momentum",
        "Showing all major Indian markets","quintal","Searching","Loading markets",
        "Live","MSP Reference","crops",
    ]

    lang_name = LANG_NAMES.get(lang, "Hindi")
    domain_note = "Crop names should be the common local/mandi name a farmer would recognize, not a literal translation."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _translation_cache)

    logger.info(f"[Translate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


@app.route("/api/translate-market/clear", methods=["POST"])
def clear_translation_cache():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production"}), 403
    _translation_cache.clear()
    return jsonify({"status": "cache cleared"})


# ─── Alerts Translation ───────────────────────────────────────────────────────
_alerts_translation_cache = {}

@app.route("/api/translate-alerts", methods=["POST"])
def translate_alerts():
    if _is_rate_limited_translate(request.remote_addr or "unknown"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.json or {}
    lang = str(data.get("lang", "en")).strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # Alert titles
        "Extreme Heat Alert", "Frost Warning", "High Fungal Disease Risk",
        "High Wind Speed Alert", "Heavy Rainfall Alert", "Thunderstorm Warning",
        "Aphid & Whitefly Risk", "Spider Mite Alert", "Crops at Risk in Current Conditions",
        # Alert messages
        "Temperature above 40°C. Provide shade netting and increase irrigation frequency.",
        "Sub-zero temperatures expected. Frost can destroy standing crops overnight.",
        "Humidity above 85% creates ideal conditions for fungal diseases.",
        "Strong winds can cause lodging in tall crops like maize and wheat.",
        "Excessive rain may cause waterlogging and root rot.",
        "Thunderstorm conditions detected. Risk of lightning and hail damage.",
        "Warm humid conditions are ideal for aphid multiplication.",
        "Hot dry conditions favour rapid spider mite population growth.",
        # Alert actions
        "Schedule irrigation every 4-5 hours. Avoid afternoon spraying.",
        "Cover crops with frost cloth. Use smudge pots or sprinkler irrigation.",
        "Apply preventive fungicide (Mancozeb 75 WP at 2.5 g/L) immediately.",
        "Avoid spraying. Support tall crops with stakes.",
        "Ensure field drainage channels are open. Pause irrigation.",
        "Stay indoors. Secure farm equipment.",
        "Spray Neem oil (5 ml/L) or Imidacloprid 0.3 ml/L at dusk.",
        "Apply Abamectin 1.8 EC (0.5 ml/L). Increase soil moisture.",
        "Consider alternate crops better suited to current climate.",
        # Alert UI labels
        "Action", "Critical", "Warning", "Advisory", "Danger",
        "All Alerts", "Warnings", "Advisories", "Weather", "Pest", "Crop Advisory",
        "Disease",
        # Pest calendar
        "Brown Plant Hopper", "Aphids", "Fall Armyworm", "Whitefly",
        "Red Spider Mite", "Stem Borer", "Thrips", "Mealy Bug",
        "Kharif (Jun–Oct)", "Rabi (Nov–Feb)", "Kharif (Jul–Sep)",
        "Year-round", "Zaid (Mar–May)", "Kharif (Jun–Sep)", "Rabi & Zaid",
        "Rice, Paddy", "Wheat, Mustard, Vegetables", "Maize, Sorghum",
        "Cotton, Tomato, Chilli", "Soybean, Cotton, Brinjal",
        "Rice, Sugarcane, Maize", "Onion, Chilli, Groundnut", "Cotton, Grapes, Papaya",
        "Feeds on rice plants causing \"hopperburn\". Thrives in humid conditions above 75%.",
        "Suck plant sap, transmit viral diseases. High risk in mild temperatures (15–25°C).",
        "Causes significant leaf damage and can destroy entire crops within days.",
        "Transmits leaf curl virus to cotton. Population explosion in dry hot weather.",
        "Causes bronzing/yellowing of leaves. Severe in hot, dry weather above 32°C.",
        "Bores into stems causing \"dead heart\" in vegetative stage and \"white ear\" at heading.",
        "Causes silvery white patches on leaves. Severe in dry weather.",

        "Forms white waxy colonies on plant parts. Excretes honeydew causing sooty mould.",
        "Use resistant varieties. Avoid excess nitrogen. Keep fields drained.",
        "Neem oil spray. Release ladybird beetles as biocontrol.",
        "Early detection critical. Bt-based bioinsecticide spray.",
        "Yellow sticky traps. Reflective mulch. Imidacloprid at threshold level.",
        "Increase irrigation. Abamectin 1.8 EC spray. Avoid dust on leaves.",
        "Pheromone traps. Chlorpyriphos 20 EC. Remove crop residues after harvest.",
        "Spinosad spray. Blue sticky traps. Avoid drought stress.",
        "Buprofezin spray. Introduce Cryptolaemus beetles as biocontrol.",
        "High", "Medium", "Low", "Risk", "Active Now", "Affects",
        # Pesticide guide
        "Chlorpyriphos 20 EC", "Imidacloprid 17.8 SL", "Mancozeb 75 WP",
        "Neem Oil 5% EC (Organic)", "Propiconazole 25 EC", "Emamectin Benzoate 5 SG",
        "Stem borer, Aphids, Termites", "Whitefly, Aphids, Brown Plant Hopper",
        "Leaf blight, Early blight, Rust, Downy mildew",
        "Aphids, Whitefly, Mites, Fungal diseases",
        "Yellow rust, Brown rust, Sheath blight",
        "Fall Armyworm, Diamond back moth, Leaf miner",
        "Target Pest", "Safe Dose", "Max Limit", "Interval", "Pre-Harvest", "PPE Required",
        "Every 14 days", "Every 21 days max", "Every 7–10 days",
        "Every 5–7 days", "Max 2 sprays per season", "Every 10–14 days",
        "15 days before harvest", "21 days before harvest", "7 days before harvest",
        "No waiting period — organic",
        "Gloves, Mask, Goggles, Full sleeve clothing",
        "Gloves, Mask, Full body protection", "Gloves, Goggles, Dust Mask",
        "Basic gloves recommended", "Full protective gear, closed shoes",
        "Full PPE, respiratory protection",
        "Highly toxic to fish and bees. Do not spray near water bodies or during flowering.",
        "Do NOT spray during bee activity (morning/evening). Highly toxic to pollinators.",
        "Causes skin and eye irritation. Do not spray on edible parts 7 days before harvest.",
        "Safe for humans and beneficial insects. May cause phytotoxicity in direct sunlight. Spray at dusk.",
        "Do not mix with alkaline pesticides. Causes groundwater contamination if overused.",
        "Highly toxic to aquatic organisms. Dispose empty containers safely. Do not reuse containers.",
        # Harmful/safe crops
        "Rice", "Wheat", "Maize", "Cotton", "Tomato", "Sugarcane",
        "Soybean", "Mustard", "Potato", "Onion", "Chilli", "Groundnut",
        "Risky", "Safe", "Suitable for", "humidity",
        "No harmful crops identified for current conditions.",
        "No fully safe crops identified — check crop calendar.",
        # Risk chart
        "Heat Stress", "Humidity Risk", "Wind Damage", "Pest Risk",
        "Disease Risk", "Pest Activity", "Overall Risk", "Current Risk Level (%)",
        # Reason strings
        "Too cold (min 10°C needed)", "Too cold (min 13°C needed)",
        "Too cold (min 18°C needed)", "Too cold (min 20°C needed)",
        "Too cold (min 22°C needed)", "Too cold (min 24°C needed)",
        "Too cold (min 25°C needed)",
        "Too hot (max 22°C tolerated)", "Too hot (max 25°C tolerated)",
        "Too hot (max 28°C tolerated)", "Too hot (max 30°C tolerated)",
        "Too hot (max 32°C tolerated)", "Too hot (max 35°C tolerated)",
        "Too hot (max 36°C tolerated)", "Too hot (max 38°C tolerated)",
        "Too hot (max 40°C tolerated)",
        "Humidity too low (min 40% needed)", "Humidity too low (min 50% needed)",
        "Humidity too low (min 60% needed)", "Humidity too low (min 70% needed)",
        "Humidity too low (min 75% needed)",
    ]

    domain_note = (
        "This is for an agricultural alerts page for Indian farmers. "
        "Translate accurately preserving technical terms like pesticide names, "
        "dosage numbers, and units (ml/L, g/L, EC, WP, SL, SG, °C, %) in their original form."
    )
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _alerts_translation_cache)

    logger.info(f"[AlertsTranslate] {len(translations)} terms for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

# ─── Dashboard Translation ────────────────────────────────────────────────────
_dashboard_translation_cache = {}

@app.route("/api/translate-dashboard", methods=["POST"])
def translate_dashboard():
    if _is_rate_limited_translate(request.remote_addr or "unknown"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.json or {}
    lang = str(data.get("lang", "en")).strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # UI Labels
        "Dashboard","Diagnose Crop","Market Prices","Alerts","Get My Location",
        "Location Found","Fetching weather...","Awaiting location...",
        "Current Weather Conditions","Live data from your location",
        "6-Day Forecast","Temperature","Humidity","Wind","Visibility","Pressure",
        "Feels like","Calm","Light breeze","Moderate breeze","Strong breeze","Storm warning",
        "Crop Recommendations","Based on your climate & location",
        "Season","Water Need","Expected Yield","Duration","Soil Type","Fertilizer",
        "Estimated Profit","Match",
        "Crop Advisory Calendar","Week-by-week action plan for your crops",
        "Pesticide & Pest Control Guide","Safe and effective crop protection plan",
        "Quick Actions","Diagnose Crop Disease","Upload or take a photo of your crop",
        "Check Market Prices","Live mandi prices across India",
        "View Active Alerts","Weather & pest warnings for your area",
        "Empowering farmers with AI-driven precision agriculture",
        "Eco-Friendly","Chemical","Week",
        # Seasons
        "Kharif (Monsoon)","Rabi (Winter)","Zaid (Summer)",
        # Water levels
        "Very High","High","Medium","Low",
        # Activity types
        "preparation","sowing","irrigation","fertilizer","maintenance","pesticide","harvest",
        # Calendar activities
        "Soil preparation & ploughing","Seed treatment & sowing","First irrigation",
        "Apply basal fertilizer (NPK)","Weeding & thinning","Apply Urea (top dressing)",
        "Pest & disease inspection","Spray fungicide if required",
        "Foliar spray micronutrients","Pre-harvest irrigation stop","Harvest preparation",
        # Crop names
        "Rice","Wheat","Maize","Cotton","Tomato","Sugarcane","Soybean","Mustard",
        # Crop descriptions
        "Ideal for high humidity and warm conditions",
        "Best suited for cool, dry winters",
        "Versatile crop for warm humid weather",
        "Thrives in hot dry spells with moderate rain",
        "High value crop for moderate climates",
        "Requires hot climate and heavy rainfall",
        "Nitrogen-fixing legume for warm monsoon",
        "Cool weather oil seed crop",
        # Soil types
        "Clay loam, alluvial","Well-drained loam","Sandy loam to clay loam",
        "Black cotton soil","Sandy loam, rich organic matter","Deep loam, good drainage",
        "Well-drained loam","Sandy loam, well-drained",
        # Pest names
        "Brown Plant Hopper","Leaf folder","Aphids","Yellow rust",
        "Fall Armyworm","Stem borer","Bollworm","Whitefly",
        # Pesticide section labels
        "Pest Control Plan","Crop","Timing",
    ]

    domain_note = "Crop, pest, and field-activity names should be the common name farmers actually use, not a literal translation."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _dashboard_translation_cache)

    logger.info(f"[DashboardTranslate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

# ─── Diagnose Page Translation (static UI text) ───────────────────────────────

_diagnose_translation_cache = {}

@app.route("/api/translate-diagnose", methods=["POST"])
def translate_diagnose():
    if _is_rate_limited_translate(request.remote_addr or "unknown"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.json or {}
    lang = str(data.get("lang", "en")).strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # Upload panel
        "Drop your crop image here", "Supports JPG, PNG, WEBP — max 10 MB",
        "Upload Photo", "Take Photo", "Image ready for analysis",
        "Remove image", "Close camera", "Capture photo", "Analyze Crop",
        "Analyzing…", "Try Again",
        # Tips card
        "Photo Tips for Best Results",
        "Focus on the most visibly affected area",
        "Use natural daylight — avoid harsh shadows",
        "Include both healthy and affected parts if possible",
        "Keep the camera steady and close (30–50 cm)",
        # Results placeholder
        "Upload a crop image to begin diagnosis",
        "Our AI will identify the disease and suggest eco-friendly treatments",
        "Upload or capture image", "Click Analyze Crop", "Get instant AI diagnosis",
        # Analyzing loader
        "AI is analyzing your crop…",
        "Identifying disease patterns and preparing remedies",
        "Scanning image…", "Detecting patterns…", "Finding remedies…",
        # Results content section headers
        "Cause", "Recovery Timeline", "Eco-Friendly Remedies", "RECOMMENDED",
        "Remedy Effectiveness Chart", "Chemical Treatment Options",
        "Prevention Tips", "Confidence", "Severity", "effectiveness",
        "AI-generated diagnosis for guidance only. Consult a local agronomist for critical crop decisions.",
        "Unknown Disease",
        # Error / failure states
        "Analysis Failed", "Could not process the image.",
        "Make sure your API key is set and the image is clear.",
        "Diagnosis failed. Please try again.",
        "Please upload or capture a crop image first.",
        "Please drop a valid image file (JPG, PNG, WEBP).",
        "Image too large. Max 10 MB allowed.",
        "Camera access denied or not available.",
        "Camera ready — position your crop in frame.",
        "Diagnosis complete!",
        # Severity levels (also used as data values from Groq)
        "Mild", "Moderate", "Severe",
        # How It Works section
        "How It Works", "Capture or Upload",
        "Take a clear photo of the affected crop leaf, stem, or fruit",
        "AI Analysis",
        "Our AI model analyzes visual patterns to identify diseases with high accuracy",
        "Get Remedies",
        "Receive eco-friendly and chemical treatment plans with dosage details instantly",
        # Pre-classifier rejection state
        "Not a Crop Photo",
        "We couldn't detect a plant, leaf, stem, fruit, or root in this image.",
        "Please retake the photo focused closely on the affected crop part.",
        "Retake Photo",
        "That doesn't look like a crop photo — please retake it focused on the plant.",
        # Ensemble agreement badges
        "Cross-Checked", "Low Agreement", "Alternate possibility",
        "Confirmed by multiple independent AI analysis passes",
        "Two independent AI passes disagreed, so confidence was lowered.",
    ]

    domain_note = "This is UI copy and section labels for a crop-disease-diagnosis app. Keep tone simple and clear for farmers; keep numbers/units/file types (JPG, PNG, WEBP, MB, cm) unchanged."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _diagnose_translation_cache)

    logger.info(f"[DiagnoseTranslate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


# ─── Dynamic Diagnosis Result Translation ─────────────────────────────────────
_diagnosis_result_cache = {}

@app.route("/api/translate-diagnosis-result", methods=["POST"])
def translate_diagnosis_result():
    if _is_rate_limited_translate(request.remote_addr or "unknown"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.json or {}
    lang   = str(data.get("lang", "en")).strip().lower()
    result = data.get("result") or {}

    if lang == "en" or not result:
        return jsonify({"lang": "en", "translations": {}})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    terms = []
    def add(val):
        if isinstance(val, str) and val.strip() and val not in terms:
            terms.append(val.strip())

    add(result.get("disease"))
    add(result.get("severity"))
    add(result.get("affected_part"))
    add(result.get("cause"))
    add(result.get("recovery_timeline"))
    for r in result.get("eco_remedies") or []:
        add(r.get("remedy")); add(r.get("method")); add(r.get("frequency"))
    for c in result.get("chemical_remedies") or []:
        add(c.get("name")); add(c.get("interval"))
        add(c.get("dose"))
    for tip in result.get("prevention") or []:
        add(tip)

    if not terms:
        return jsonify({"lang": lang, "translations": {}})
    if sum(len(t) for t in terms) > 4000:
        return jsonify({"lang": lang, "translations": {}})

    domain_note = ("This is an AI-generated crop disease diagnosis for a farmer. "
                   "Translate naturally using terms a farmer would recognize. "
                   "Keep chemical/brand names, numbers, and units (kg/ha, Rs, days, ml/L, g/ha, "
                   "quintal, %, SL, EC, SC, WP, SG, NPK) unchanged.")
    cache_key = lang + "::" + "|".join(terms)
    translations, cached = _translate_terms(terms, lang_name, domain_note, cache_key, _diagnosis_result_cache)

    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=(DEBUG_MODE and os.getenv("SPACE_ID") is None))
