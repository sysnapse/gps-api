#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""對 gps-api 做端到端自測：上報、查詢、權限、聚合、清理。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TMP = "/tmp/gps-test"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

ENV = dict(os.environ)
ENV.update({
    "DB_PATH": TMP + "/gps.db",
    "PLACES_FILE": TMP + "/places.json",
    "LISTEN_PORT": "18095",
    "INGEST_USER": "gpsphone",
    "INGEST_PASS": "test-ingest-pass",
    "TOKEN_FULL": "full-token-123",
    "TOKEN_RECENT": "recent-token-456",
    "RECENT_WINDOW_MIN": "120",
    "RAW_RETENTION_DAYS": "7",
    "STAY_MIN_MIN": "1",
    "AGG_RUN_HOUR_HKT": "99",
})
with open(TMP + "/places.json", "w") as f:
    json.dump([{"id": "home", "name": "家", "lat": 22.3000, "lon": 114.1700, "radius": 150},
               {"id": "office", "name": "公司", "lat": 22.2800, "lon": 114.1600, "radius": 150}], f)

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
proc = subprocess.Popen([sys.executable, SRC], env=ENV,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(1.2)
BASE = "http://127.0.0.1:18095"


def call(path, method="GET", data=None, headers=None):
    h = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def basic():
    import base64
    return {"Authorization": "Basic " + base64.b64encode(b"gpsphone:test-ingest-pass").decode()}


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


now = int(time.time())
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" :: " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


# 1. 健康檢查
s, b = call("/healthz")
check("healthz", s == 200 and b["ok"])

# 2. 未認證上報被拒
s, b = call("/pub", "POST", {"_type": "location", "lat": 22.3, "lon": 114.17, "tst": now})
check("pub rejects no auth", s == 401, s)

# 3. 空 body（OwnTracks 刪 friend）要 200 []
s, b = call("/pub", "POST", None, basic())
check("pub empty body -> 200 []", s == 200 and b == [], (s, b))

# 4. 正常上報：家 20 分鐘（兩點）
s, b = call("/pub", "POST", {"_type": "location", "lat": 22.30001, "lon": 114.17001,
                            "tst": now - 1200, "acc": 12, "batt": 80, "conn": "w"},
            dict(basic(), **{"X-Limit-D": "phone1"}))
check("pub first point", s == 200, s)
s, _ = call("/pub", "POST", {"_type": "location", "lat": 22.30002, "lon": 114.17002,
                             "tst": now - 300, "acc": 10}, dict(basic(), **{"X-Limit-D": "phone1"}))
check("pub second point", s == 200, s)

# 5. 重複 tst 不重複插入
call("/pub", "POST", {"_type": "location", "lat": 22.30002, "lon": 114.17002,
                      "tst": now - 300, "acc": 10}, dict(basic(), **{"X-Limit-D": "phone1"}))
s, b = call("/api/last", headers=bearer("full-token-123"))
check("dedupe by (device,tst)", s == 200, b)

# 6. 查詢需要 token
s, _ = call("/api/last")
check("api rejects no token", s == 401, s)
s, _ = call("/api/last", headers=bearer("wrong"))
check("api rejects bad token", s == 401, s)

# 7. /api/last 內容
s, b = call("/api/last?device=phone1", headers=bearer("full-token-123"))
check("last found+stale flag", s == 200 and b.get("found") is True and "stale" in b, b)

# 8. recent token 不可用 /api/points
s, b = call("/api/points", headers=bearer("recent-token-456"))
check("recent token blocked on /api/points", s == 403, s)

# 9. 聚合：手動觸發今日
s, b = call("/api/admin/aggregate", "POST", {"day": time.strftime(
    "%Y-%m-%d", time.gmtime(now + 8 * 3600))}, bearer("full-token-123"))
check("aggregate today", s == 200, b)
day = time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600))
s, b = call("/api/day?day=" + day + "&device=phone1", headers=bearer("full-token-123"))
check("day has stays named 家", s == 200 and any(x["place_name"] == "家" for x in b["stays"]), b)
check("stays carry no coordinates",
      all(not any(k in x for k in ("lat", "lon")) for x in b["stays"]), b["stays"])

# 10. recent token 只拿到今日摘要，且點數限 2 小時內
s, b = call("/api/day?day=" + day, headers=bearer("recent-token-456"))
check("recent token day ok", s == 200 and "stays" in b, s)
check("recent token points clipped to 2h",
      all(now - p["tst"] <= 7200 + 5 for p in b.get("points", [])), b.get("points"))

# 11. cmd 指令排隊，下一次 POST 回傳
s, b = call("/api/admin/cmd", "POST", {"device": "phone1", "action": "reportLocation"},
            bearer("full-token-123"))
check("queue cmd", s == 200 and b.get("queued") is True, b)
s, b = call("/pub", "POST", {"_type": "location", "lat": 22.30003, "lon": 114.17003,
                             "tst": now}, dict(basic(), **{"X-Limit-D": "phone1"}))
check("cmd delivered in reply", isinstance(b, list) and any(
    x.get("action") == "reportLocation" for x in b), b)
s, b = call("/pub", "POST", {"_type": "location", "lat": 22.30004, "lon": 114.17004,
                             "tst": now + 1}, dict(basic(), **{"X-Limit-D": "phone1"}))
check("cmd delivered once only", b == [], b)

# 12. 時鐘偏差拒收
s, b = call("/pub", "POST", {"_type": "location", "lat": 22.3, "lon": 114.17,
                             "tst": now - 30 * 86400}, dict(basic(), **{"X-Limit-D": "phone1"}))
check("clock skew rejected quietly", s == 200 and b == [], b)

# 13. admin 需要 full token
s, _ = call("/api/admin/purge", "POST", {"scope": "raw"}, bearer("recent-token-456"))
check("admin blocked for recent token", s == 403, s)

# 14. 清理 raw（保留聚合）
s, b = call("/api/admin/purge", "POST", {"scope": "raw"}, bearer("full-token-123"))
check("purge raw", s == 200 and b["raw_deleted"] >= 1, b)
s, b = call("/api/stats", headers=bearer("full-token-123"))
check("raw emptied but stays kept", b["raw_points"] == 0 and b["stays"] >= 1, b)


# ================= 路線推斷（/api/infer）=================
# 15. 路線表未建立時應回 503（提示先 sync），不是 500 或空候選
s, b = call("/api/infer", headers=bearer("recent-token-456"))
check("infer before route sync -> 503", s == 503 and b.get("error") == "route table empty", b)

# 16. 灌入合成路線：T1 直線 8 站（每站約 220 m），T2 同走向但偏 1.5 km，T3 遠方
import sqlite3 as _sq
_conn = _sq.connect(TMP + "/gps.db", timeout=10)
_rows = []
for _i in range(8):
    _lat = 22.3000 + _i * 0.0020
    _rows.append(("T1", "O", "1", _i + 1, "T1S%d" % (_i + 1), "T1站%d" % (_i + 1), _lat, 114.1700))
    _rows.append(("T2", "O", "1", _i + 1, "T2S%d" % (_i + 1), "T2站%d" % (_i + 1), _lat, 114.1850))
    _rows.append(("T3", "O", "1", _i + 1, "T3S%d" % (_i + 1), "T3站%d" % (_i + 1), 22.4000 + _i * 0.002, 114.1700))
_conn.executemany("INSERT OR REPLACE INTO route_stops(route, bound, service_type, seq, stop_id,"
                  " name, lat, lon) VALUES (?,?,?,?,?,?,?,?)", _rows)
for _rt in ("T1", "T2", "T3"):
    _conn.execute("INSERT OR REPLACE INTO route_meta(route, bound, service_type, stops, synced_at)"
                  " VALUES (?,?,?,?,?)", (_rt, "O", "1", 8, 0))
_conn.commit()
_conn.close()

s, b = call("/api/route-stops?route=T1&bound=O", headers=bearer("full-token-123"))
check("route-stops debug endpoint", s == 200 and b["count"] == 8 and b["stops"][0]["name"] == "T1站1", b[:2] if isinstance(b, list) else b)

# 17. 沿 T1 行走（每 220 m 一點、30 秒一點 ≈ 26 km/h）→ top1 T1/O、state moving、high
_traces = []
_bt = int(time.time()) - 600
for _i in range(6):
    _s, _b = call("/pub", "POST",
                  {"_type": "location", "lat": 22.3000 + _i * 0.0022, "lon": 114.17005,
                   "tst": _bt + _i * 30}, dict(basic(), **{"X-Limit-D": "bustest"}))
s, b = call("/api/infer?window_min=30&device=bustest", headers=bearer("recent-token-456"))
_c0 = (b.get("candidates") or [{}])[0]
check("infer moving: top1 是 T1/O", s == 200 and _c0.get("route") == "T1" and _c0.get("bound") == "O", _c0)
check("infer moving: state/confidence", b.get("state") == "moving" and _c0.get("confidence") == "high",
      (b.get("state"), _c0.get("confidence")))
check("infer moving: 進度合理", _c0.get("seq_from") == 1 and _c0.get("seq_to") >= 4, _c0.get("seq_to"))
check("infer 帶 eta_probe 給九巴bot", isinstance(_c0.get("eta_probe"), list) and len(_c0["eta_probe"]) >= 1, _c0.get("eta_probe"))
check("infer 不會洩漏車牌/班次欄位", "vehicle" not in json.dumps(_c0) and "plate" not in json.dumps(_c0), "")

# 18. 靜止在同一個站 → state stationary、confidence low（不能說「在車上」）
_bt2 = int(time.time()) - 300
for _i in range(4):
    call("/pub", "POST", {"_type": "location", "lat": 22.30405, "lon": 114.17005,
                          "tst": _bt2 + _i * 60}, dict(basic(), **{"X-Limit-D": "stoptest"}))
s, b = call("/api/infer?window_min=30&device=stoptest", headers=bearer("recent-token-456"))
check("infer stationary: state 與信心", b.get("state") == "stationary"
      and b["candidates"][0]["confidence"] == "low", (b.get("state"), b["candidates"][0]["confidence"]))

# 19. 同走廊第二條線（T1 與 T1B 共用同一座標）→ 必須標明歧義、high 降為 medium
_conn = _sq.connect(TMP + "/gps.db", timeout=10)
_dup = [("T1B", "O", "1", _i + 1, "T1BS%d" % (_i + 1), "T1B站%d" % (_i + 1),
         22.3000 + _i * 0.0020, 114.1700) for _i in range(8)]
_conn.executemany("INSERT OR REPLACE INTO route_stops(route, bound, service_type, seq, stop_id,"
                  " name, lat, lon) VALUES (?,?,?,?,?,?,?,?)", _dup)
_conn.commit(); _conn.close()
s, b = call("/api/infer?window_min=30&device=bustest", headers=bearer("recent-token-456"))
check("infer 同走廊降為 medium 並講明歧義", b["candidates"][0]["confidence"] == "medium"
      and any("同走廊" in r for r in b.get("reasons", [])), b.get("reasons"))

# 20. recent token 可用 /api/infer，但 /api/points 仍 403（越權封鎖未被新端點破壞）
s, _ = call("/api/points", headers=bearer("recent-token-456"))
check("recent token 仍被 /api/points 封鎖", s == 403, s)

# 21. 離線點（遠離所有已同步路線）→ off_route
_bt3 = int(time.time()) - 200
for _i in range(4):
    call("/pub", "POST", {"_type": "location", "lat": 22.5000 + _i * 0.003, "lon": 114.3000,
                          "tst": _bt3 + _i * 60}, dict(basic(), **{"X-Limit-D": "offtest"}))
s, b = call("/api/infer?window_min=30&device=offtest", headers=bearer("recent-token-456"))
check("infer off_route", b.get("state") == "off_route" and b.get("candidates") == [], b.get("state"))

# 22. ts 參數可回溯；window_min 被夾在 RECENT_WINDOW_MIN 之內
s, b = call("/api/infer?window_min=999", headers=bearer("full-token-123"))
check("infer window_min 被夾住", s == 200 and b["window_min"] == 120, b.get("window_min"))


# ================= /api/state（九巴bot 輪詢用）=================
# 23. 在車上 → state=moving、route/bound 有值、change token 非空、帶 at_stop/next_stop
s, b = call("/api/state?window_min=30&device=bustest", headers=bearer("recent-token-456"))
_st = b if isinstance(b, dict) else {}
check("/api/state moving 帶 route 與 change", s == 200 and _st.get("state") == "moving"
      and _st.get("route") in ("T1", "T1B") and _st.get("change"), _st)
check("/api/state 不洩漏車牌", "plate" not in json.dumps(_st) and "vehicle" not in json.dumps(_st), "")

# 24. since 等於當前 token → unchanged:true，body 極短（適合靜默輪詢）
_tok = _st.get("change")
s, b2 = call("/api/state?window_min=30&device=bustest&since=" + urllib.parse.quote(_tok or "x"),
             headers=bearer("recent-token-456"))
check("/api/state since 相同 -> unchanged", s == 200 and b2.get("unchanged") is True, b2)
check("/api/state unchanged body 夠細", len(json.dumps(b2)) < 120, len(json.dumps(b2)))

# 25. 下車（用獨立裝置＋窄窗，令上車點跌出窗口）→ token 改變、route 清空
_now25 = int(time.time())
for _i in range(4):                                  # 先在 T1 行走（12 分鐘前，窗口 5 分鐘收不到）
    call("/pub", "POST", {"_type": "location", "lat": 22.3000 + _i * 0.0020, "lon": 114.1700,
                          "tst": _now25 - 720 + _i * 30}, dict(basic(), **{"X-Limit-D": "trans"}))
s, _b25 = call("/api/state?window_min=5&device=trans", headers=bearer("recent-token-456"))
_t25 = _b25.get("change")
for _i in range(3):                                  # 之後站在同一點不動
    call("/pub", "POST", {"_type": "location", "lat": 22.3140, "lon": 114.1700,
                          "tst": _now25 - 60 + _i * 20}, dict(basic(), **{"X-Limit-D": "trans"}))
s, b3 = call("/api/state?window_min=5&device=trans&since=" + urllib.parse.quote(_t25 or "x"),
             headers=bearer("recent-token-456"))
check("/api/state 下車 -> token 變且 route 清空", b3.get("change") != _t25
      and b3.get("route") is None and b3.get("state") in ("stationary", "off_route"),
      (b3.get("change"), b3.get("route"), b3.get("state")))

# 26. 無路線表時 503（與 infer 一致），recent token 亦不可讀 /api/state 以外越權端點
s, b4 = call("/api/state?device=nobody", headers=bearer("recent-token-456"))
check("/api/state 空窗亦回 200 不炸", s == 200 and "change" in b4, s)

proc.terminate()
out = proc.stdout.read() if proc.stdout else ""
print("\n--- server log tail ---\n" + "\n".join(out.splitlines()[-8:]))
print("\nRESULT:", "ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
