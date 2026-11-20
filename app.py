#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gps-api —— 手機 GPS 上報與查詢服務（單一檔案、只用 Python 標準庫）

角色：
  1. 接收 OwnTracks（Android，HTTP 模式）嘅位置 POST（/pub，HTTP Basic 認證）
  2. 原樣存入 SQLite（原始層，預設保留 7 日）
  3. 每日生成「日聚合摘要」（地點名稱 + 停留分鐘，唔存座標，永久保留）
  4. 提供受限查詢 API（Bearer token，分 full / recent 兩種權限）

環境變數見 README.md。整個檔案冇第三方依賴，Python 3.11+ 即可。
"""

import base64
import hmac
import json
import math
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 設定

HKT_OFFSET = 8 * 3600                      # Asia/Hong_Kong，無夏令時間
DB_PATH = os.environ.get("DB_PATH", "/data/gps.db")
PLACES_FILE = os.environ.get("PLACES_FILE", "/data/places.json")
INGEST_USER = os.environ.get("INGEST_USER", "")
INGEST_PASS = os.environ.get("INGEST_PASS", "")
TOKEN_FULL = os.environ.get("TOKEN_FULL", "")
TOKEN_RECENT = os.environ.get("TOKEN_RECENT", "")
RECENT_WINDOW_MIN = int(os.environ.get("RECENT_WINDOW_MIN", "120"))
RAW_RETENTION_DAYS = int(os.environ.get("RAW_RETENTION_DAYS", "7"))
KEEP_AGGREGATES = os.environ.get("KEEP_AGGREGATES", "1") == "1"
AGG_RUN_HOUR_HKT = int(os.environ.get("AGG_RUN_HOUR_HKT", "3"))
GEOCODE = os.environ.get("GEOCODE", "0") == "1"
GEOCODE_UA = os.environ.get("GEOCODE_UA", "gps-api/1.0 (personal use)")
STAY_RADIUS_M = float(os.environ.get("STAY_RADIUS_M", "180"))
STAY_GAP_MIN = float(os.environ.get("STAY_GAP_MIN", "30"))
STAY_MIN_MIN = float(os.environ.get("STAY_MIN_MIN", "5"))
MAX_BODY = 64 * 1024
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8095"))

# ---- 路線推斷（KMB／LWB 開放數據，2026-11-20 加）
INFER_ENABLED = os.environ.get("INFER_ENABLED", "1") == "1"
INFER_WINDOW_MIN = int(os.environ.get("INFER_WINDOW_MIN", "45"))
INFER_MAX_OFF_M = float(os.environ.get("INFER_MAX_OFF_M", "300"))
INFER_MIN_SPEED_KMH = float(os.environ.get("INFER_MIN_SPEED_KMH", "12"))
INFER_MIN_SPAN_M = float(os.environ.get("INFER_MIN_SPAN_M", "400"))
INFER_ROUTES = [r.strip() for r in os.environ.get("INFER_ROUTES", "").split(",") if r.strip()]
SYNC_PAUSE_S = float(os.environ.get("SYNC_PAUSE_S", "0.2"))
KMB_API = os.environ.get("KMB_API", "https://data.etabus.gov.hk/v1/transport/kmb")

DB_LOCK = threading.Lock()


# ---------------------------------------------------------------- 工具

def now() -> int:
    return int(time.time())


def hkt_day(ts: int) -> str:
    """把 UTC epoch 轉成 HKT 日期字串 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d", time.gmtime(ts + HKT_OFFSET))


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def load_places():
    try:
        with open(PLACES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if "lat" in p and "lon" in p]
    except Exception:
        return []


def match_place(lat: float, lon: float):
    for p in load_places():
        if haversine_m(lat, lon, float(p["lat"]), float(p["lon"])) <= float(p.get("radius", 150)):
            return p.get("id") or p.get("name"), p.get("name") or p.get("id")
    return None, None


_geocode_cache = {}


def reverse_geocode(lat: float, lon: float):
    """用 Nominatim 取人類可讀地名。只在聚合時呼叫，失敗回 None。"""
    key = (round(lat, 3), round(lon, 3))
    if key in _geocode_cache:
        return _geocode_cache[key]
    if not GEOCODE:
        return None
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"format": "jsonv2", "zoom": 17, "accept-language": "zh-Hant", "lat": lat, "lon": lon})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        name = (d.get("name") or d.get("display_name", "").split(",")[0]).strip() or None
    except Exception:
        name = None
    _geocode_cache[key] = name
    time.sleep(1.1)          # Nominatim 禮貌間隔：1 req/s
    return name


# ---------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_points (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device      TEXT NOT NULL,
  tst         INTEGER NOT NULL,          -- 手機回報的時間（UTC epoch）
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  acc         REAL,
  vel         REAL,
  batt        INTEGER,
  conn        TEXT,
  tid         TEXT,
  received_at INTEGER NOT NULL,          -- 伺服器收到時間
  day         TEXT NOT NULL,             -- HKT 日期
  UNIQUE(device, tst)
);
CREATE INDEX IF NOT EXISTS idx_raw_day ON raw_points(device, day, tst);

CREATE TABLE IF NOT EXISTS days (
  day          TEXT NOT NULL,
  device       TEXT NOT NULL,
  generated_at INTEGER NOT NULL,
  points       INTEGER NOT NULL,
  first_tst    INTEGER,
  last_tst     INTEGER,
  distance_km  REAL,
  stay_minutes INTEGER,
  PRIMARY KEY (day, device)
);

-- 日聚合停留：只有地點名稱與時間，長期層不含任何座標
CREATE TABLE IF NOT EXISTS stays (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  day        TEXT NOT NULL,
  device     TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  place_id   TEXT,
  place_name TEXT,
  source     TEXT,                       -- geofence / geocode / unnamed
  arrive_tst INTEGER NOT NULL,
  leave_tst  INTEGER NOT NULL,
  minutes    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stays_day ON stays(day, device, seq);

CREATE TABLE IF NOT EXISTS cmd_queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device       TEXT NOT NULL,
  action       TEXT NOT NULL,
  payload      TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  delivered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cmd_pending ON cmd_queue(device, delivered_at);

CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  actor  TEXT,
  action TEXT,
  detail TEXT
);
"""


# 路線站序快取（由 /api/admin/sync-routes 從 KMB 開放數據建立；不含任何個人資料）
SCHEMA2 = """
CREATE TABLE IF NOT EXISTS route_stops (
  route        TEXT NOT NULL,
  bound        TEXT NOT NULL,          -- O 或 I
  service_type TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  stop_id      TEXT,
  name         TEXT,
  lat          REAL NOT NULL,
  lon          REAL NOT NULL,
  PRIMARY KEY (route, bound, service_type, seq)
);
CREATE INDEX IF NOT EXISTS idx_route_stops_stop ON route_stops(stop_id);
CREATE TABLE IF NOT EXISTS route_meta (
  route        TEXT NOT NULL,
  bound        TEXT NOT NULL,
  service_type TEXT NOT NULL,
  stops        INTEGER,
  synced_at    INTEGER,
  PRIMARY KEY (route, bound, service_type)
);
CREATE TABLE IF NOT EXISTS route_sync (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER,
  routes TEXT,
  rows   INTEGER,
  ok     INTEGER,
  detail TEXT
);
"""


def init_db():
    with DB_LOCK, db() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA2)


def audit(conn, actor, action, detail=""):
    conn.execute("INSERT INTO audit(ts, actor, action, detail) VALUES (?,?,?,?)",
                 (now(), actor, action, detail[:500]))


# ---------------------------------------------------------------- 聚合與清理

def compute_day(day: str):
    """由原始點生成某日的停留摘要與里程，覆寫該日舊結果。"""
    with DB_LOCK, db() as conn:
        devices = [r["device"] for r in conn.execute(
            "SELECT DISTINCT device FROM raw_points WHERE day=? ORDER BY device", (day,))]
        for device in devices:
            rows = list(conn.execute(
                "SELECT tst, lat, lon, acc FROM raw_points WHERE day=? AND device=? ORDER BY tst",
                (day, device)))
            if not rows:
                continue
            stays = []
            cur = None
            for r in rows:
                if cur is None:
                    cur = {"n": 1, "lat": r["lat"], "lon": r["lon"], "t0": r["tst"], "t1": r["tst"]}
                    continue
                moved = haversine_m(cur["lat"] / cur["n"], cur["lon"] / cur["n"], r["lat"], r["lon"])
                gap = r["tst"] - cur["t1"]
                if moved <= STAY_RADIUS_M and gap <= STAY_GAP_MIN * 60:
                    cur["n"] += 1
                    cur["lat"] += r["lat"]
                    cur["lon"] += r["lon"]
                    cur["t1"] = r["tst"]
                else:
                    stays.append(cur)
                    cur = {"n": 1, "lat": r["lat"], "lon": r["lon"], "t0": r["tst"], "t1": r["tst"]}
            if cur:
                stays.append(cur)

            kept = []
            for s in stays:
                minutes = int(round((s["t1"] - s["t0"]) / 60))
                if minutes < STAY_MIN_MIN:
                    continue                     # 路過不算停留
                clat, clon = s["lat"] / s["n"], s["lon"] / s["n"]
                pid, pname = match_place(clat, clon)
                source = "geofence"
                if not pname:
                    pname = reverse_geocode(clat, clon)
                    source = "geocode" if pname else "unnamed"
                kept.append((pid, pname, source, s["t0"], s["t1"], minutes))

            distance = 0.0
            prev = None
            for r in rows:
                if prev and 0 < r["tst"] - prev[2] <= 600 and (r["acc"] or 999) <= 120:
                    distance += haversine_m(prev[0], prev[1], r["lat"], r["lon"])
                prev = (r["lat"], r["lon"], r["tst"])
            stay_minutes = sum(k[5] for k in kept)

            conn.execute("DELETE FROM stays WHERE day=? AND device=?", (day, device))
            for i, (pid, pname, source, t0, t1, minutes) in enumerate(kept, 1):
                conn.execute(
                    "INSERT INTO stays(day, device, seq, place_id, place_name, source,"
                    " arrive_tst, leave_tst, minutes) VALUES (?,?,?,?,?,?,?,?,?)",
                    (day, device, i, pid, pname, source, t0, t1, minutes))
            conn.execute(
                "INSERT OR REPLACE INTO days(day, device, generated_at, points, first_tst,"
                " last_tst, distance_km, stay_minutes) VALUES (?,?,?,?,?,?,?,?)",
                (day, device, now(), len(rows), rows[0]["tst"], rows[-1]["tst"],
                 round(distance / 1000, 2), stay_minutes))
    return day


def purge_raw():
    cutoff = now() - RAW_RETENTION_DAYS * 86400
    with DB_LOCK, db() as conn:
        cur = conn.execute("DELETE FROM raw_points WHERE tst < ?", (cutoff,))
        n = cur.rowcount
        if n:
            audit(conn, "scheduler", "purge_raw", "deleted %d points older than %d days"
                  % (n, RAW_RETENTION_DAYS))
    return n


def scheduler():
    """每分鐘檢查一次：HKT 03:xx 生成昨日與今日聚合，然後清理過期原始點。"""
    done = None
    while True:
        try:
            t = time.gmtime(now() + HKT_OFFSET)
            day = time.strftime("%Y-%m-%d", t)
            if t.tm_hour == AGG_RUN_HOUR_HKT and done != day:
                yesterday = hkt_day(now() - 86400)
                compute_day(yesterday)
                compute_day(day)
                purge_raw()
                if not KEEP_AGGREGATES:
                    with DB_LOCK, db() as conn:
                        conn.execute("DELETE FROM stays WHERE day < ?", (hkt_day(now() - 90 * 86400),))
                done = day
        except Exception as e:                      # scheduler 不可死
            print("scheduler error:", e, flush=True)
        time.sleep(60)



# ---------------------------------------------------------------- 路線推斷
# 目標：由最近軌跡判斷「正在哪條路線、哪個方向、走到第幾站」。
# 明確不做：不識別車牌與班次（KMB 開放數據沒有車輛 GPS，任何聲稱認得班次的做法都是猜）。

BOUND_WORD = {"O": "outbound", "I": "inbound"}


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def dbear(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def seg_dist_m(lat: float, lon: float, a: dict, b: dict) -> float:
    """點到站間線段的距離（等距投影，香港尺度誤差可忽略）。"""
    lat0 = math.radians((a["lat"] + b["lat"]) / 2)

    def xy(la, lo):
        return (math.radians(lo) * lat0 * 6371000.0, math.radians(la) * 6371000.0)

    px, py = xy(lat, lon)
    ax, ay = xy(a["lat"], a["lon"])
    bx, by = xy(b["lat"], b["lon"])
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _http_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "gps-api/1.0 (personal use)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sync_routes(routes, service_types=("1", "2")):
    """從 KMB 開放數據抓指定路線的站序，寫入 route_stops。回傳統計。"""
    routes = [r for r in routes if r]
    if not routes:
        return {"ok": False, "error": "routes 空；請帶 routes 或在環境變數設 INFER_ROUTES"}
    stops = {}
    sl = _http_json("%s/stop/" % KMB_API)
    for s in sl.get("data", []):
        stops[s["stop"]] = s
    rows = 0
    got = []
    for route in routes:
        for bd, word in BOUND_WORD.items():
            for st in service_types:
                try:
                    d = _http_json("%s/route-stop/%s/%s/%s"
                                   % (KMB_API, urllib.parse.quote(route), word, st))
                except Exception:
                    continue                      # 該路線沒有這個 service_type，屬正常
                data = d.get("data") or []
                if not data:
                    continue
                with DB_LOCK, db() as conn:
                    conn.execute("DELETE FROM route_stops WHERE route=? AND bound=? AND service_type=?",
                                 (route, bd, st))
                    for r in data:
                        s = stops.get(r["stop"])
                        if not s:
                            continue
                        conn.execute(
                            "INSERT OR REPLACE INTO route_stops(route, bound, service_type, seq,"
                            " stop_id, name, lat, lon) VALUES (?,?,?,?,?,?,?,?)",
                            (route, bd, st, int(r["seq"]), r["stop"],
                             s.get("name_tc") or s.get("name_en") or "",
                             float(s["lat"]), float(s["long"])))
                        rows += 1
                    conn.execute("INSERT OR REPLACE INTO route_meta(route, bound, service_type,"
                                 " stops, synced_at) VALUES (?,?,?,?,?)",
                                 (route, bd, st, len(data), now()))
                got.append("%s/%s/%s(%d)" % (route, bd, st, len(data)))
                time.sleep(SYNC_PAUSE_S)
    with DB_LOCK, db() as conn:
        conn.execute("INSERT INTO route_sync(ts, routes, rows, ok, detail) VALUES (?,?,?,?,?)",
                     (now(), ",".join(routes), rows, 1 if rows else 0, " ".join(got)[:400]))
        audit(conn, "api", "sync_routes", "%d routes, %d rows" % (len(routes), rows))
    return {"ok": bool(rows), "rows": rows, "series": got, "stops_known": len(stops)}


def load_route_table(conn):
    """讀出 route_stops，組成 {(route,bound,st): [ {seq,lat,lon,name,stop_id}, ... ]}。"""
    table = {}
    for r in conn.execute("SELECT route, bound, service_type, seq, stop_id, name, lat, lon"
                          " FROM route_stops ORDER BY route, bound, service_type, seq"):
        table.setdefault((r["route"], r["bound"], r["service_type"]), []).append(dict(r))
    return table


def _nearest(rs, lat, lon):
    """回傳 (最近站距, 最近站索引, 到線段最短距離)。"""
    best_d, best_i = 1e9, 0
    for i, s in enumerate(rs):
        d = haversine_m(lat, lon, s["lat"], s["lon"])
        if d < best_d:
            best_d, best_i = d, i
    perp = best_d
    for j in (best_i - 1, best_i):
        if 0 <= j < len(rs) - 1:
            d = seg_dist_m(lat, lon, rs[j], rs[j + 1])
            if d < perp:
                perp = d
    return best_d, best_i, perp


def infer_candidates(points, table):
    """points: 時間升序的 [{tst, lat, lon, ...}]。回傳 (state, speed, span, candidates, reasons)。"""
    if not points:
        return "unknown", 0.0, 0.0, [], ["沒有任何點"]
    if len(points) == 1:
        span, kmh, maxp = 0.0, 0.0, 0.0
    else:
        span, maxp = 0.0, 0.0
        for i in range(1, len(points)):
            a, b = points[i - 1], points[i]
            d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            span += d
            dt = max(1, b["tst"] - a["tst"])
            maxp = max(maxp, d / dt * 3.6)
        dt_all = max(1, points[-1]["tst"] - points[0]["tst"])
        kmh = span / dt_all * 3.6
    if len(points) < 2:
        return "unknown", kmh, span, [], ["只有 1 個點，無法判斷方向"]
    moving = span >= INFER_MIN_SPAN_M and (maxp >= INFER_MIN_SPEED_KMH or kmh >= INFER_MIN_SPEED_KMH)

    cands = []
    for key, rs in table.items():
        if len(rs) < 3:
            continue
        idxs, dists, cover = [], [], 0
        for p in points:
            d_stop, i, perp = _nearest(rs, p["lat"], p["lon"])
            idxs.append(i)
            dists.append(min(d_stop, perp))
            if perp <= 150:
                cover += 1
        mean_d = sum(dists) / len(dists)
        if mean_d > INFER_MAX_OFF_M:
            continue
        coverage = cover / len(points)
        checks, oks = 0, 0.0
        for k in range(1, len(points)):
            a, b = points[k - 1], points[k]
            if haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) < 50:
                continue
            checks += 1
            ub = bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])
            lo = rs[max(0, idxs[k] - 2)]
            hi = rs[min(len(rs) - 1, idxs[k] + 2)]
            rb = bearing_deg(lo["lat"], lo["lon"], hi["lat"], hi["lon"])
            d = dbear(ub, rb)
            if d <= 45:
                oks += 1
            elif d <= 90:
                oks += 0.5
        bearing_s = (oks / checks) if checks else 0.5
        inc = sum(1 for k in range(1, len(idxs)) if idxs[k] >= idxs[k - 1])
        mono = inc / max(1, len(idxs) - 1)
        score = (0.45 * max(0.0, 1.0 - mean_d / INFER_MAX_OFF_M)
                 + 0.20 * coverage + 0.20 * bearing_s + 0.15 * mono)
        i_now = idxs[-1]
        nxt = rs[min(len(rs) - 1, i_now + 1)]
        ahead = rs[i_now + 1:i_now + 4]
        cands.append({
            "route": key[0], "bound": key[1], "service_type": key[2],
            "score": round(score, 3),
            "dist_m": round(mean_d, 1),
            "coverage": round(coverage, 2),
            "bearing_ok": round(bearing_s, 2),
            "seq_mono": round(mono, 2),
            "seq_from": rs[min(idxs)]["seq"],
            "seq_to": rs[max(idxs)]["seq"],
            "at_stop": {"seq": rs[i_now]["seq"], "stop_id": rs[i_now]["stop_id"],
                        "name": rs[i_now]["name"]},
            "next_stop": {"seq": nxt["seq"], "stop_id": nxt["stop_id"], "name": nxt["name"]},
            "stops_total": len(rs),
            "eta_probe": [{"seq": s["seq"], "stop_id": s["stop_id"], "name": s["name"]}
                          for s in ahead],
        })
    cands.sort(key=lambda c: -c["score"])
    cands = cands[:5]
    reasons = []
    if not cands:
        return "off_route", kmh, span, [], ["所有已同步路線的沿線距離都超出 %.0f m" % INFER_MAX_OFF_M]
    top = cands[0]
    gap = top["score"] - (cands[1]["score"] if len(cands) > 1 else 0.0)
    ambiguous = [c for c in cands[1:] if top["score"] - c["score"] < 0.05]
    if not moving:
        conf = "low"
        reasons.append("位移 %.0f m、最高段速 %.1f km/h，未達在車上門檻；只能說「在該路線沿線」，不能說「在車上」"
                       % (span, maxp))
        state = "stationary"
    else:
        state = "moving"
        if (top["score"] >= 0.80 and top["coverage"] >= 0.8
                and top["seq_mono"] >= 0.8 and gap >= 0.08):
            conf = "high"
        elif top["score"] >= 0.62:
            conf = "medium"
        else:
            conf = "low"
        reasons.append("沿線距 %.0f m、覆蓋 %.0f%%、方向一致 %.0f%%、站序單調 %.0f%%"
                       % (top["dist_m"], top["coverage"] * 100,
                          top["bearing_ok"] * 100, top["seq_mono"] * 100))
    if ambiguous:
        reasons.append("與 %s 分數差 <0.05（同走廊），須用 ETA 交叉核對"
                       % "、".join("%s/%s" % (c["route"], c["bound"]) for c in ambiguous))
        if conf == "high":
            conf = "medium"
    top["confidence"] = conf
    top["gap"] = round(gap, 3)
    for c in cands[1:]:
        c["confidence"] = "low"
    return state, kmh, span, cands, reasons


# ---------------------------------------------------------------- HTTP

def _basic_ok(header: str) -> bool:
    if not header or not header.lower().startswith("basic ") or not INGEST_PASS:
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
        user, _, pw = raw.partition(":")
    except Exception:
        return False
    ok_u = hmac.compare_digest(user, INGEST_USER)
    ok_p = hmac.compare_digest(pw, INGEST_PASS)
    return ok_u and ok_p


def _role(header: str):
    if not header or not header.lower().startswith("bearer "):
        return None
    tok = header.split(" ", 1)[1].strip()
    if TOKEN_FULL and hmac.compare_digest(tok, TOKEN_FULL):
        return "full"
    if TOKEN_RECENT and hmac.compare_digest(tok, TOKEN_RECENT):
        return "recent"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "gps-api/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 只記錄方法、路徑、狀態，永不記錄請求內容或認證
        print("%s %s" % (self.command, self.path.split("?")[0]), flush=True)

    # ---- 回應工具
    def send_json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n)

    # ---- POST /pub（手機上報）
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/pub":
            return self.handle_pub()
        if path.startswith("/api/admin/"):
            role = _role(self.headers.get("Authorization", ""))
            if role != "full":
                return self.send_json(403, {"error": "full token required"})
            return self.handle_admin(path[len("/api/admin/"):])
        return self.send_json(404, {"error": "not found"})

    def handle_pub(self):
        if not _basic_ok(self.headers.get("Authorization", "")):
            return self.send_json(401, {"error": "unauthorized"})
        try:
            body = self.read_body()
        except ValueError:
            return self.send_json(413, {"error": "too large"})
        if not body:                                   # OwnTracks 刪 friend 會送空 body
            return self.send_json(200, [])

        # 使用者與裝置：優先用 header，其次查詢參數
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        device = (self.headers.get("X-Limit-D") or (q.get("d") or [""])[0] or "phone").strip()[:40]
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return self.send_json(400, {"error": "bad json"})
        if not isinstance(data, dict):
            return self.send_json(400, {"error": "bad payload"})

        reply = []
        if data.get("_type") == "location" and "lat" in data and "lon" in data and "tst" in data:
            ts = int(data["tst"])
            if abs(ts - now()) > 7 * 86400:            # 時鐘嚴重偏差，記 audit 但仍然拒收
                with DB_LOCK, db() as conn:
                    audit(conn, device, "reject_skew", "tst=%d" % ts)
                return self.send_json(200, [])
            with DB_LOCK, db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO raw_points(device, tst, lat, lon, acc, vel, batt,"
                    " conn, tid, received_at, day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (device, ts, float(data["lat"]), float(data["lon"]), data.get("acc"),
                     data.get("vel"), data.get("batt"), data.get("conn"),
                     (data.get("tid") or "")[:4], now(), hkt_day(ts)))
                row = conn.execute(
                    "SELECT id FROM cmd_queue WHERE device=? AND delivered_at IS NULL"
                    " ORDER BY id LIMIT 1", (device,)).fetchone()
                if row:
                    payload = json.loads(conn.execute(
                        "SELECT payload FROM cmd_queue WHERE id=?", (row["id"],)).fetchone()["payload"])
                    conn.execute("UPDATE cmd_queue SET delivered_at=? WHERE id=?", (now(), row["id"]))
                    reply.append(payload)

        return self.send_json(200, reply)

    def handle_admin(self, what: str):
        try:
            body = self.read_body()
            args = json.loads(body.decode() or "{}") if body else {}
        except Exception:
            return self.send_json(400, {"error": "bad json"})
        if what == "cmd":                               # 排一個 reportLocation 指令
            device = str(args.get("device", "phone"))[:40]
            action = str(args.get("action", "reportLocation"))
            payload = json.dumps({"_type": "cmd", "action": action})
            with DB_LOCK, db() as conn:
                conn.execute("INSERT INTO cmd_queue(device, action, payload, created_at)"
                             " VALUES (?,?,?,?)", (device, action, payload, now()))
                audit(conn, "api", "queue_cmd", "%s %s" % (device, action))
            return self.send_json(200, {"queued": True, "device": device, "action": action,
                                       "note": "會在手機下一次 POST 時回傳，最長一個上報間隔"})
        if what == "sync-routes":                         # 從 KMB 開放數據更新路線站序
            routes = args.get("routes") or INFER_ROUTES
            sts = tuple(str(x) for x in (args.get("service_types") or ["1", "2"]))
            try:
                res = sync_routes(routes, sts)
            except Exception as e:
                return self.send_json(502, {"error": "sync failed: %s" % e})
            return self.send_json(200, res)
        if what == "aggregate":                          # 手動補算某日
            day = args.get("day") or hkt_day(now() - 86400)
            compute_day(day)
            return self.send_json(200, {"day": day, "done": True})
        if what == "purge":                              # 手動刪除（含聚合，不可逆）
            scope = args.get("scope", "raw")
            before = args.get("before")
            with DB_LOCK, db() as conn:
                n1 = n2 = 0
                if scope in ("raw", "both"):
                    sql = "DELETE FROM raw_points"
                    params = ()
                    if before:
                        sql += " WHERE tst < ?"
                        params = (int(before),)
                    n1 = conn.execute(sql, params).rowcount
                if scope in ("aggregates", "both"):
                    sql = "DELETE FROM stays"
                    params = ()
                    if before:
                        sql += " WHERE day < ?"
                        params = (str(before),)
                    n2 = conn.execute(sql, params).rowcount
                audit(conn, "api", "purge", "scope=%s before=%s raw=%d agg=%d"
                      % (scope, before, n1, n2))
            return self.send_json(200, {"raw_deleted": n1, "stays_deleted": n2})
        return self.send_json(404, {"error": "unknown admin action"})

    # ---- GET 查詢
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "time": now()})
        if not path.startswith("/api/"):
            return self.send_json(404, {"error": "not found"})
        role = _role(self.headers.get("Authorization", ""))
        if not role:
            return self.send_json(401, {"error": "bearer token required"})
        q = urllib.parse.parse_qs(parsed.query)
        try:
            return self.handle_query(path, q, role)
        except Exception as e:
            return self.send_json(500, {"error": str(e)})

    def handle_query(self, path, q, role):
        recent_floor = now() - RECENT_WINDOW_MIN * 60

        def one(name, default=None):
            v = q.get(name)
            return v[0] if v else default

        if path == "/api/last":
            device = one("device")
            with DB_LOCK, db() as conn:
                sql = ("SELECT device, tst, lat, lon, acc, vel, batt, day FROM raw_points"
                       " WHERE tst >= ?")
                params = [recent_floor if role == "recent" else 0]
                if device:
                    sql += " AND device = ?"
                    params.append(device)
                sql += " ORDER BY tst DESC LIMIT 1"
                row = conn.execute(sql, params).fetchone()
            if not row:
                return self.send_json(200, {"found": False,
                                           "hint": "最近 %d 分鐘內沒有上報" % RECENT_WINDOW_MIN})
            d = dict(row)
            d["found"] = True
            d["age_seconds"] = now() - d["tst"]
            d["stale"] = d["age_seconds"] > 1800
            d["hkt"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(d["tst"] + HKT_OFFSET))
            return self.send_json(200, d)

        if path == "/api/today":
            day = hkt_day(now())
            return self.day_payload(day, one("device"), role)

        if path == "/api/day":
            day = one("day") or hkt_day(now() - 86400)
            if len(day) != 10:
                return self.send_json(400, {"error": "day must be YYYY-MM-DD"})
            return self.day_payload(day, one("device"), role)

        if path == "/api/points":
            if role != "full":
                return self.send_json(403, {"error": "full token required"})
            frm = int(one("from") or (now() - 86400))
            to = int(one("to") or now())
            device = one("device")
            with DB_LOCK, db() as conn:
                sql = ("SELECT device, tst, lat, lon, acc, vel, batt FROM raw_points"
                       " WHERE tst BETWEEN ? AND ?")
                params = [frm, to]
                if device:
                    sql += " AND device = ?"
                    params.append(device)
                sql += " ORDER BY tst"
                rows = [dict(r) for r in conn.execute(sql, params)]
            return self.send_json(200, {"count": len(rows), "points": rows})

        if path == "/api/route-stops":
            route = one("route")
            if not route:
                return self.send_json(400, {"error": "route required"})
            bd = (one("bound") or "O").upper()[:1]
            st = one("service_type") or "1"
            with DB_LOCK, db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT seq, stop_id, name, lat, lon FROM route_stops"
                    " WHERE route=? AND bound=? AND service_type=? ORDER BY seq",
                    (route, bd, st))]
                meta = conn.execute("SELECT * FROM route_meta WHERE route=? AND bound=?"
                                    " AND service_type=?", (route, bd, st)).fetchone()
            return self.send_json(200, {"route": route, "bound": bd, "service_type": st,
                                        "count": len(rows), "synced_at": meta["synced_at"] if meta else None,
                                        "stops": rows})

        if path == "/api/infer":
            if not INFER_ENABLED:
                return self.send_json(503, {"error": "inference disabled"})
            try:
                win = int(one("window_min") or INFER_WINDOW_MIN)
            except ValueError:
                return self.send_json(400, {"error": "window_min must be integer"})
            win = max(5, min(win, RECENT_WINDOW_MIN))
            try:
                ts = int(one("ts") or now())
            except ValueError:
                return self.send_json(400, {"error": "ts must be epoch seconds"})
            device = one("device")
            frm = ts - win * 60
            if role == "recent":
                frm = max(frm, now() - RECENT_WINDOW_MIN * 60, ts - RECENT_WINDOW_MIN * 60)
            with DB_LOCK, db() as conn:
                sql = ("SELECT tst, lat, lon, acc, vel FROM raw_points WHERE tst BETWEEN ? AND ?")
                params = [frm, ts]
                if device:
                    sql += " AND device = ?"
                    params.append(device)
                sql += " ORDER BY tst"
                points = [dict(r) for r in conn.execute(sql, params)]
                table = load_route_table(conn)
            if not table:
                return self.send_json(503, {"error": "route table empty",
                                            "hint": "POST /api/admin/sync-routes 先建立路線站序"})
            state, kmh, span, cands, reasons = infer_candidates(points, table)
            last = points[-1] if points else None
            place = match_place(last["lat"], last["lon"]) if last else None
            out = {"ts": ts, "window_min": win, "points_used": len(points),
                   "state": state,
                   "speed_kmh": round(kmh, 1),
                   "span_m": round(span),
                   "place": place,
                   "candidates": cands,
                   "reasons": reasons,
                   "disclaimer": "只判斷路線／方向／進度，不識別車牌或班次；方向代碼 O=outbound、I=inbound",
                   "hkt": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + HKT_OFFSET))}
            if last:
                out["point"] = {"tst": last["tst"], "lat": last["lat"], "lon": last["lon"],
                                "acc": last["acc"], "age_seconds": now() - last["tst"]}
            if role == "recent":
                out["points"] = [{"tst": p["tst"], "lat": p["lat"], "lon": p["lon"]} for p in points]
            return self.send_json(200, out)

        if path == "/api/state":
            # 給九巴bot 輪詢的極簡端點：只回「我而家喺邊架車」，帶 change token。
            # since=<token> 且未變 → 回 {"unchanged": true}（幾十字節），適合靜默 watch。
            if not INFER_ENABLED:
                return self.send_json(503, {"error": "inference disabled"})
            try:
                win = int(one("window_min") or INFER_WINDOW_MIN)
            except ValueError:
                return self.send_json(400, {"error": "window_min must be integer"})
            win = max(5, min(win, RECENT_WINDOW_MIN))
            device = one("device")
            since = one("since")
            ts = now()
            frm = ts - win * 60
            if role == "recent":
                frm = max(frm, ts - RECENT_WINDOW_MIN * 60)
            with DB_LOCK, db() as conn:
                sql = "SELECT tst, lat, lon, acc, vel FROM raw_points WHERE tst BETWEEN ? AND ?"
                params = [frm, ts]
                if device:
                    sql += " AND device = ?"
                    params.append(device)
                sql += " ORDER BY tst"
                points = [dict(r) for r in conn.execute(sql, params)]
                table = load_route_table(conn)
            if not table:
                return self.send_json(503, {"error": "route table empty",
                                            "hint": "POST /api/admin/sync-routes 先建立路線站序"})
            state, kmh, span, cands, reasons = infer_candidates(points, table)
            last = points[-1] if points else None
            top = cands[0] if cands else None
            route = top["route"] if top else None
            bound = top["bound"] if top else None
            silent = (state != "moving") or top is None or top.get("confidence") == "low"
            if silent:
                route = bound = None            # 未上車／未確定 → token 不含路線，避免自我觸發
            token = "|".join([state, route or "-", bound or "-"])
            age = (now() - last["tst"]) if last else None
            if since and since == token:
                return self.send_json(200, {"unchanged": True, "change": token, "ts": ts})
            out = {"ts": ts, "change": token, "state": state, "speed_kmh": round(kmh, 1),
                   "points_used": len(points), "age_seconds": age,
                   "stale": bool(age is None or age > 1800),
                   "route": route, "bound": bound,
                   "confidence": (top or {}).get("confidence"),
                   "at_stop": (top or {}).get("at_stop"),
                   "next_stop": (top or {}).get("next_stop"),
                   "seq_from": (top or {}).get("seq_from"),
                   "seq_to": (top or {}).get("seq_to"),
                   "place": match_place(last["lat"], last["lon"]) if last else None,
                   "alts": [{"route": c["route"], "bound": c["bound"],
                             "confidence": c.get("confidence")} for c in cands[1:4]],
                   "reasons": reasons,
                   "hkt": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + HKT_OFFSET)),
                   "disclaimer": "只判斷路線／方向／進度，不識別車牌或班次"}
            return self.send_json(200, out)

        if path == "/api/days":
            if role != "full":
                return self.send_json(403, {"error": "full token required"})
            frm = one("from") or hkt_day(now() - 30 * 86400)
            to = one("to") or hkt_day(now())
            with DB_LOCK, db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM days WHERE day BETWEEN ? AND ? ORDER BY day", (frm, to))]
            return self.send_json(200, {"count": len(rows), "days": rows})

        if path == "/api/stats":
            if role != "full":
                return self.send_json(403, {"error": "full token required"})
            with DB_LOCK, db() as conn:
                raw = conn.execute("SELECT COUNT(*) c, MIN(tst) a, MAX(tst) b FROM raw_points").fetchone()
                st = conn.execute("SELECT COUNT(*) c FROM stays").fetchone()
            return self.send_json(200, {"raw_points": raw["c"], "raw_oldest": raw["a"],
                                       "raw_newest": raw["b"], "stays": st["c"],
                                       "raw_retention_days": RAW_RETENTION_DAYS,
                                       "keep_aggregates": KEEP_AGGREGATES})
        return self.send_json(404, {"error": "unknown endpoint"})

    def day_payload(self, day, device, role):
        with DB_LOCK, db() as conn:
            dsql = "SELECT * FROM days WHERE day = ?"
            sparams = [day]
            params = [day]
            if device:
                dsql += " AND device = ?"
                sparams.append(device)
            drow = conn.execute(dsql, sparams).fetchone()
            ssql = "SELECT seq, place_id, place_name, source, arrive_tst, leave_tst, minutes" \
                   " FROM stays WHERE day = ?"
            if device:
                ssql += " AND device = ?"
                params.append(device)
            ssql += " ORDER BY seq"
            stays = [dict(r) for r in conn.execute(ssql, params)]
            pcount = None
            points = []
            if role == "full":
                pcount = conn.execute(
                    "SELECT COUNT(*) c FROM raw_points WHERE day = ?", (day,)).fetchone()["c"]
            else:
                # recent token：今日摘要 + 最近 2 小時原始點，不得回溯更早
                if day == hkt_day(now()):
                    points = [dict(r) for r in conn.execute(
                        "SELECT tst, lat, lon, acc FROM raw_points WHERE day = ? AND tst >= ?"
                        " ORDER BY tst", (day, now() - RECENT_WINDOW_MIN * 60))]
        for s in stays:
            s["arrive_hkt"] = time.strftime("%H:%M", time.gmtime(s["arrive_tst"] + HKT_OFFSET))
            s["leave_hkt"] = time.strftime("%H:%M", time.gmtime(s["leave_tst"] + HKT_OFFSET))
        out = {"day": day, "summary": dict(drow) if drow else None,
               "stays": stays, "raw_point_count": pcount}
        if role == "recent":
            out["points"] = points
        return self.send_json(200, out)


def main():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    print("gps-api listening on 0.0.0.0:%d, db=%s, raw_retention=%d days"
          % (LISTEN_PORT, DB_PATH, RAW_RETENTION_DAYS), flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
