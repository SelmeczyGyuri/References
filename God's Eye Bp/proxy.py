"""
God's Eye Budapest – Proxy
API: POST https://kozutfigyelo-back.budapestkozut.hu/api/ikszr/camerahistoryimage
     POST https://kozutfigyelo-back.budapestkozut.hu/api/ikszr/get  (egyedi kamera info)
"""

from flask import Flask, Response, abort, send_from_directory, jsonify
from flask_cors import CORS
import requests, os, base64, threading, time
from datetime import datetime

app = Flask(__name__)
CORS(app)

UPSTREAM   = "https://kozutfigyelo-back.budapestkozut.hu"
IMAGE_PATH = "/api/ikszr/camerahistoryimage"
GET_PATH   = "/api/ikszr/get"

HTML_FILE = "gebp_vc.html"
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    "Referer":      "https://kozutfigyelo.budapestkozut.hu/",
    "Origin":       "https://kozutfigyelo.budapestkozut.hu",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept":       "application/json, */*",
}

# Ismert kamera ID-k (10001-10085 + néhány magasabb amit a böngészőből láttunk)
BASE_IDS = list(range(10001, 10086)) + [10439]

# Kamera adat gyorsítótár: {id: {lat, lng, name, district, active}}
_cam_cache = {}
_cache_lock = threading.Lock()
_cache_ready = False


def fetch_cam_info(cam_id):
    """Egyedi kamera adat lekérése az API-tól."""
    body = {"objectType": "cameras", "externalId": str(cam_id)}
    try:
        resp = requests.post(UPSTREAM + GET_PATH, headers=HEADERS, json=body, timeout=6)
        data = resp.json()
        if data.get("success") and data.get("data", {}).get("instance"):
            inst = data["data"]["instance"]
            return {
                "id":       cam_id,
                "name":     inst.get("location_description", f"Kamera {cam_id}"),
                "lat":      float(inst.get("latitude", 0)),
                "lng":      float(inst.get("longitude", 0)),
                "active":   inst.get("is_active", False),
                "status":   inst.get("status", "unknown"),
                "last_data": inst.get("last_data", ""),
            }
    except Exception as e:
        pass
    return None


def build_cache():
    """Háttérszálon lekéri az összes kamera adatát."""
    global _cache_ready
    print("[CACHE] Kamera adatok lekérése indul...")
    loaded = 0
    for cam_id in BASE_IDS:
        info = fetch_cam_info(cam_id)
        if info:
            with _cache_lock:
                _cam_cache[cam_id] = info
            loaded += 1
        time.sleep(0.05)  # 50ms delay – ne terheljük az API-t
    _cache_ready = True
    print(f"[CACHE] Kész: {loaded}/{len(BASE_IDS)} kamera betöltve")


# Gyorsítótár építése háttérszálon induláskor
threading.Thread(target=build_cache, daemon=True).start()


@app.route("/")
def index():
    path = os.path.join(BASE_DIR, HTML_FILE)
    if not os.path.exists(path):
        return f"<pre>HIBA: '{HTML_FILE}' nem talalhato: {BASE_DIR}</pre>", 404
    return send_from_directory(BASE_DIR, HTML_FILE)


@app.route("/cam/<cam_id>")
def proxy_cam(cam_id):
    now  = datetime.now().strftime("%H:%M")
    body = {"time": now, "externalId": cam_id}
    url  = UPSTREAM + IMAGE_PATH
    try:
        resp = requests.post(url, headers=HEADERS, json=body, timeout=8)
        print(f"[CAM {cam_id}] HTTP {resp.status_code} | {len(resp.content)}b")
        if resp.status_code != 200:
            abort(resp.status_code)
        data = resp.json()
        if data.get("success") is False:
            print(f"[CAM {cam_id}] API hiba: {data.get('message')}")
            abort(502)
        b64 = data.get("data", {}).get("image")
        if not b64:
            print(f"[CAM {cam_id}] Nincs kép a válaszban")
            abort(502)
        img = base64.b64decode(b64)
        print(f"[CAM {cam_id}] OK {len(img)}b")
        return Response(img, content_type="image/jpeg",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
    except requests.exceptions.Timeout:
        abort(504)
    except Exception as e:
        print(f"[CAM {cam_id}] Hiba: {e}")
        abort(502)


@app.route("/caminfo/<cam_id>")
def cam_info(cam_id):
    """Egyedi kamera adat – cache-ből vagy frissen."""
    cam_id_int = int(cam_id)
    with _cache_lock:
        cached = _cam_cache.get(cam_id_int)
    if cached:
        return jsonify({"success": True, "data": cached, "source": "cache"})
    # Nem volt cache-ben – lekérjük frissen
    info = fetch_cam_info(cam_id_int)
    if info:
        with _cache_lock:
            _cam_cache[cam_id_int] = info
        return jsonify({"success": True, "data": info, "source": "live"})
    return jsonify({"success": False, "error": "Nem talalhato"}), 404


@app.route("/camlist")
def cam_list():
    """Összes gyorsítótárazott kamera adata."""
    with _cache_lock:
        cams = list(_cam_cache.values())
    return jsonify({
        "success":     True,
        "ready":       _cache_ready,
        "count":       len(cams),
        "cameras":     cams,
    })


@app.route("/debug/<cam_id>")
def debug_cam(cam_id):
    now  = datetime.now().strftime("%H:%M")
    body = {"time": now, "externalId": cam_id}
    try:
        resp = requests.post(UPSTREAM + IMAGE_PATH, headers=HEADERS, json=body, timeout=8)
        data = resp.json()
        b64  = data.get("data", {}).get("image", "")
        img  = base64.b64decode(b64) if b64 else None
        return jsonify({
            "cam_id":          cam_id,
            "request_body":    body,
            "status_code":     resp.status_code,
            "success":         data.get("success"),
            "message":         data.get("message"),
            "image_extracted": img is not None,
            "image_bytes":     len(img) if img else 0,
            "b64_length":      len(b64),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    with _cache_lock:
        cache_count = len(_cam_cache)
    return jsonify({
        "status":       "ok",
        "upstream":     UPSTREAM + IMAGE_PATH,
        "cache_ready":  _cache_ready,
        "cache_count":  cache_count,
        "time_now":     datetime.now().strftime("%H:%M"),
    })


if __name__ == "__main__":
    print("=" * 65)
    print("  GOD'S EYE BUDAPEST – PROXY")
    print(f"  POST {UPSTREAM}{IMAGE_PATH}")
    print("=" * 65)
    print("  App:     http://localhost:5000")
    print("  Camlist: http://localhost:5000/camlist")
    print("  Debug:   http://localhost:5000/debug/10439")
    print("  Health:  http://localhost:5000/health")
    print("=" * 65)
    app.run(host="0.0.0.0", port=5000, debug=False)