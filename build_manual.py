#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 prose + 實測過的檔案內容組裝成 shared/gps-上報-設置手冊.md。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "gps-上報-設置手冊.md")


def read(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return f.read().rstrip()


HEAD = r"""# 手機 GPS 自動上報：設置手冊

最後更新：2026-11-20 08:0x HKT（GPS 設置 bot）——按 `shared/長遠規劃/架構筆記.md` 的 2026-11-18 實測更正入口層。
狀態：**程式碼已寫成並在本機完整自測通過（34 項全 pass，含路線推斷）；推斷算法已用真實 KMB 開放數據驗證；尚未在你硬件上部署。**
部署位置：Pi 4B（家居，`192.168.1.100`），Docker。
可部署檔案：`shared/gps-docker/`（`app.py`、`docker-compose.yml`、`traefik-gps.yml`、`places.example.json`、`selftest.py`）。

**本版兩項架構更正（來源：架構筆記 §8.1，2026-11-18 21:29–21:32 實測）**

1. Pi 4B 的對外終結者是 **Traefik**（`192.168.1.100:443`），不是 nginx。原 2.4 節的 nginx server block 作廢，改寫成 Traefik router。
2. `gps.example.com` **不是全新 hostname**——2026-11-18 21:30 對外探測回 **444**，即 DNS 記錄已在、缺回源。原 2.5 節「新增 hostname」改為「補回源」。

## 0. 已拍板的四項決定（owner 2026-11-13）

| # | 問題 | 決定 | 可逆性 |
|---|---|---|---|
| 1 | 手機平台 | **Android** | — |
| 2 | 托管與入口 | **Pi 4B Docker ＋ 沿用現有 Cloudflare 入口**（`gps.example.com` 記錄已存在，只補回源），不開新端口、不另建 tunnel | 刪回源與容器即完全回退 |
| 3 | 保留期 | **兩層：原始點 7 日；日聚合摘要永久**（長期層不存座標） | 改短即時刪過期；改長不會追回已刪 |
| 4 | 誰可讀 | **兩個 token**：`full`（Mine／未來蹤跡bot，可讀全量）、`recent`（九巴bot，只可讀最近 2 小時原始點＋今日摘要） | 撤銷 token 即失效 |

沿革：本設計取代 `docs/gps-location-architecture.md`（Mine 2026-11-12 草案）內的 Mosquitto＋ot-recorder＋MQTT 8883 方案。改動理由：owner 決定不開新端口，MQTT 裸 TCP 必然需要額外端口；HTTP 模式只需一個 HTTPS 路徑，直接複用現有入口。草案中「低頻墊底、按需精細、7 日保留」三項原則保留。

---

## 1. 架構與資料流

```
┌─────────────────────┐
│ Android 手機        │  OwnTracks（HTTP 模式）
│ OwnTracks app       │  POST /pub  JSON + HTTP Basic
│ 5 分鐘／100 米      │  離線時本機排隊，有網即補送
└──────────┬──────────┘
           │ HTTPS（TLS 由 Cloudflare 提供）
           ▼
┌──────────────────────────────────────────────┐
│ Cloudflare（既有 zone example.com）         │
│ gps.example.com（現時回 444＝缺回源）       │
│ /pub 不加 Access 政策；可加 rate limit       │
└──────────┬───────────────────────────────────┘
           │ 二選一：Tunnel ingress／proxied A＋Origin Rule 8443
           │ 不新增端口、不新建 tunnel
           ▼
┌──────────────────────────────────────────────┐
│ Pi 4B 192.168.1.100                         │
│ Traefik（:443，對外終結者；實測非 nginx）    │
│   Host=gps.example.com → gps-api:8095       │
│                                              │
│ docker: gps-api（python:3.12-slim，單檔）    │
│   ├─ 寫入 raw_points（原始層，7 日）         │
│   ├─ 每日 03:xx HKT 生成 stays/days（永久）  │
│   ├─ 每日 03:xx 刪 7 日以上原始點            │
│   └─ /api/* 查詢，Bearer token 分 full/recent│
│ volume: /srv/gps/data/gps.db（SQLite WAL）   │
└──────────────────────────────────────────────┘
           │ HTTPS + Bearer
           ▼
┌──────────────────────────────────────────────┐
│ 讀取方：Mine（full）、九巴bot（recent）      │
│ 未來蹤跡bot（full）                          │
└──────────────────────────────────────────────┘
```

### 資料流（一筆上報）

1. 手機每 5 分鐘或移動 100 米取一次點，POST `https://gps.example.com/pub`，body 是 OwnTracks 標準 JSON（`_type: location`，含 `lat/lon/tst/acc/batt/conn`）。
2. Cloudflare 回源到 Pi 4B（Tunnel 或 WAN 8443→Traefik:443），Traefik 以 Host 分流到 `gps-api:8095`；app 只認 `/pub`、`/healthz`、`/api/*`，其餘 404。
3. `gps-api` 用 HTTP Basic 驗證（帳號 `gpsphone` ＋ 長隨機密碼）；通過則 `INSERT OR IGNORE`（`device + tst` 唯一，重送不會重複）。
4. 回應固定是 JSON 陣列；若佇列有指令（例如 `reportLocation`），就在這次回應帶回去，手機即時補一個點。
5. 每日 03:xx HKT：把前一日與當日原始點聚合成停留（地點名＋到達／離開＋分鐘），然後刪 7 日前原始點。聚合層不含任何座標。

### 為什麼選這套（與被否決方案對比）

| 方案 | 否決理由 |
|---|---|
| Cloudflare Worker ＋ D1 | owner 2026-11-13 明確表示不需要，走 Pi docker |
| MQTT（Mosquitto）＋ ot-recorder | 裸 MQTT 要額外開 8883，與「不開新端口」衝突 |
| Traccar／Dawarich／Hauk | 要 Postgres／Redis／Java，資源與維護成本高；Dawarich 不能遠端改間隔 |
| 只用 MacroDroid 自寫 | 要自己處理離線排隊與省電，不如 OwnTracks 成熟；保留為備選 |

### 為何不放 Pi 5 或新的 agent VLAN 30（2026-11-19 補充）

- **不放 Pi 5（20.x，public）**：20.x → 10.x 本身不通（2026-11-11 實測三例全 HTTP 000）。放 Pi 5 雖令讀取端（bot 都在 Pi 5）零成本，但手機入口要另拉一條到 20.x 的通道（WAN 443 已指向 Pi 5），等於多一套對外入口，暴露面增加而收益只是省一次跨段查詢。
- **不放 minipc／VLAN 30（agent 段）**：owner 2026-11-18 拍板方向是 agent → home 拒、public → agent 拒，只有 home → agent 准。放進去即令 Pi 5 上的 bot 讀不到自己的資料庫，且家居入口要壓在那台實驗機上。
- **結論**：DB 與 API 留在 10.x 的 Pi 4B；bot 端一律經 `gps.example.com`，與兩級 token 設計一致。

關鍵取捨：**OwnTracks HTTP 模式不支援遠端 `setConfiguration`**（官方文件列出的可回傳指令只有 `dump`、`reportLocation`、`reportSteps`、`action`、`setWaypoints`）。所以「按需把間隔切成 60 秒」在 HTTP 模式下做不到；替代是 `POST /api/admin/cmd` 排一個 `reportLocation`，手機下一次上報時立刻補一個即時點（最長等一個上報間隔，即 5 分鐘）。九巴bot 場景需要的是「上車前一個準確點」，這個替代足夠；若日後真的要遠端改間隔，才需要另建 MQTT 通道。

---

## 2. 部署步驟（Pi 4B）

以下命令由 **owner 在 Pi 4B 執行**。Pi 4B 對 bot 是唯讀，我不會代跑。

### 2.1 建目錄與放檔案

```sh
sudo mkdir -p /srv/gps/data
sudo cp app.py /srv/gps/app.py
sudo cp places.json /srv/gps/data/places.json     # 由 places.example.json 改成本人真實地點
sudo chown -R 1000:1000 /srv/gps/data
```

### 2.2 產生三個密鑰（不要自選、不要貼進聊天）

```sh
python3 -c "import secrets;print('INGEST_PASS='+secrets.token_urlsafe(32))"
python3 -c "import secrets;print('TOKEN_FULL='+secrets.token_urlsafe(32))"
python3 -c "import secrets;print('TOKEN_RECENT='+secrets.token_urlsafe(32))"
```

寫入 `/srv/gps/.env`（`chmod 600`，不要進 git）：

```
INGEST_PASS=...
TOKEN_FULL=...
TOKEN_RECENT=...
```

`docker-compose.yml` 放在 `/srv/gps/docker-compose.yml`，同一個目錄執行。

### 2.3 起容器

```sh
cd /srv/gps && sudo docker compose up -d
sudo docker compose logs --tail=20 gps-api      # 應見 "gps-api listening on 0.0.0.0:8095"
curl -s http://127.0.0.1:8095/healthz           # 應回 {"ok": true, ...}
```

### 2.4 Traefik（取代原 nginx 步驟）

**前提更正**：Pi 4B 的對外終結者是 Traefik。證據（2026-11-18 21:31 外網唯讀探測）：`https://203.0.113.10:8443/` 回 `HTTP/1.1 404`、`Content-Length: 0`、**無 `Server` header**；`http://…:8443/` 回 400，body 為 `Bad Request` ＋ `This combination of host and port requires TLS.`（Traefik 固定字串；nginx 會帶 `Server: nginx/x.y`）。同刻 UniFi 只有 2 條 port forward，其中 `home-https-expose` = WAN 8443 → `192.168.1.100:443`。

先確認兩件我未量度的事：

```sh
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i traefik
docker inspect <traefik 容器> -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect <traefik 容器> -f '{{json .Config.Labels}}' | tr ',' '\n' | grep -ientrypoint
```

- **network 名**：若 Traefik 用 docker provider，`gps-api` 要與它同一個 network；本手冊的 compose 先用預設 bridge，起容器後執行一次：
  ```sh
  sudo docker network connect <上一步查到的 network> gps-api
  sudo docker restart gps-api
  ```
  這一步可逆（`docker network disconnect` 即還原）。
- **entrypoint 名**：`traefik-gps.yml` 內寫 `websecure`，若你的是 `https`／`web-secure` 就照改。

**做法**：把 `traefik-gps.yml` 放進 Traefik 的 dynamic config 目錄（常見 `/etc/traefik/dynamic/`，以你現有 provider 設定為準），Traefik 會自動載入，不需 restart。若你用 docker labels 而非 file provider，就按檔內規則改成 `gps-api` 的 labels，效果相同。

驗證（在 Pi 4B 本機，繞過 Cloudflare）：

```sh
curl -sk --resolve gps.example.com:443:127.0.0.1 https://gps.example.com/healthz
```

回 `{"ok": true, ...}` 即 Traefik → gps-api 通。**這一步未通之前，不要往 Cloudflare 那邊排查。**

pi4 內部是否另有非 443 的 nginx：未量度；若真有，它只可能是 Traefik 之後的第二層，不影響本節。

### 2.5 Cloudflare 回源（這一步要你按，我沒有你的 zone 權限）

**事實更正**：`gps.example.com` **已存在於 zone**，不是要新增。2026-11-18 21:30–21:31 對外探測：`gps`／`www`／`vikunja`／`nas` 全回 **444**（CF 收到請求但拿不到源站回應），而 `grafana`／`dsh` 回 302 到 Access、`example.com` 回 200。即缺的是**回源設定**，DNS 記錄本身在。

先確認現時屬哪種（我無 CF API token，ingress 全清單未量度）：

- Zone → DNS：`gps` 是 CNAME 到 `<uuid>.cfargotunnel.com`（＝Tunnel），還是指向家用 WAN IP 的 A／CNAME（＝proxied）？

**情況一：Tunnel**（`grafana`／`dsh` 302 到 Access，較可能屬此）

1. Zero Trust → Networks → Tunnels → 該 tunnel → Public hostname → 編輯或新增 `gps.example.com`。
2. Service 兩選一，不要兩層都繞：
   - `https://192.168.1.100:443`（＝Traefik，需在 Additional settings 開 `No TLS Verify`，`Origin Server Name` 填 `gps.example.com`）；或
   - `http://192.168.1.100:8095` 直達 gps-api（此時把 compose 的 `127.0.0.1:8095:8095` 改成 `192.168.1.100:8095:8095`，Traefik 完全不經手）。
3. **不要**為 `/pub` 加 Access 政策（手機存不了服務權杖）；認證由 Basic auth 負責。

**情況二：proxied A ＋ 端口**

1. WAN 443 已被 `public-https-expose` 佔用指向 Pi 5；Pi 4B 只有 **8443**（`home-https-expose`）。
2. 所以 `gps` 這條 proxied A 需要 **Origin Rule 改目的端口為 8443**（Rules → Origin Rules：hostname `gps.example.com` → Destination Port `8443`）。此做法**我未實測**；若你的方案不支援，就走情況一的 Tunnel。
3. 同一個 Traefik 已收 8443 進來的全部 hostname，靠 Host header 分流，**不需要新的 port forward**。

**兩種情況共用**

1. 確認 `https://gps.example.com/healthz` 從手機流動網絡回 `{"ok": true}`。
2. 可選：WAF → Rate limiting，條件 `http.host eq "gps.example.com" and http.request.uri.path eq "/pub"`，每 IP 每分鐘 60 次。
3. 可選：WAF 自訂規則封掉 `/pub`、`/healthz`、`/api/` 以外路徑（app 已 404，這是第二層）。

**邊界記錄**：不改任何現有 hostname、不改 WAF／Access 政策、不新增端口、不新建 tunnel；只補 `gps` 這條的回源。owner 2026-11-13 已明示同意沿用現有 Cloudflare 入口。

---

## 3. 完整程式碼（app.py）

單一檔案、只用 Python 標準庫（`http.server`、`sqlite3`、`urllib`），無第三方依賴、無 build step。

```python
"""

TAIL = r"""```

### 3.1 關鍵實作說明

| 決定 | 理由 |
|---|---|
| `UNIQUE(device, tst)` ＋ `INSERT OR IGNORE` | OwnTracks 斷網重送會重複 POST，這是最省事的去重 |
| 拒收與伺服器時差 > 7 日的點 | 防止手機時鐘錯亂污染日聚合；拒收但不報錯（回 200 `[]`），避免 app 無限重試 |
| 空 body 回 200 `[]` | OwnTracks 刪 friend 會送零長度 payload，官方明言應忽略 |
| 聚合只用「停留 ≥ 5 分鐘」 | 路過不停的地點不記，減少噪音與私隱表面 |
| 聚合層只寫地名與時間 | owner 要求長期層不含座標；`stays` 表無 lat/lon 欄位，結構上做不到洩漏 |
| `recent` token 在 `/api/day` 只回今日＋最近 2 小時點 | 九巴bot 只需要即時位置；不得藉查詢介面回溯歷史 |
| 日誌只印方法與路徑 | 永不記錄 payload 或認證標頭 |
| `audit` 表 | 記錄清理、排指令、拒收，出事可查；不含位置內容 |

---

## 4. docker-compose 與 Traefik

### docker-compose.yml

```yaml
"""

MID_PLACES = r"""```

### places.json（由 places.example.json 複製後改成真實座標）

```json
"""

MID_TRAEFIK = r"""```

座標請你自己從地圖取，不要貼給我。半徑 120–200 米已足夠分辨「在家」與「在路上」。

### traefik-gps.yml

```yaml
"""

MID_SCHEMA = r"""```

---

## 5. 資料庫 schema（SQLite，實際建表語句見 `app.py` 內 `SCHEMA`）

| 表 | 內容 | 保留 |
|---|---|---|
| `raw_points` | `device, tst, lat, lon, acc, vel, batt, conn, tid, received_at, day` | 7 日（cron 刪） |
| `days` | 每日匯總：點數、首末時間、里程 km、停留總分鐘 | 永久 |
| `stays` | 每次停留：`place_id, place_name, source, arrive_tst, leave_tst, minutes`（**無座標**） | 永久 |
| `cmd_queue` | 待回傳給手機的指令 | 送達即標記 |
| `audit` | 清理／排指令／拒收記錄 | 永久（不含位置） |

索引：`raw_points(device, day, tst)`、`stays(day, device, seq)`、`cmd_queue(device, delivered_at)`。
單一檔案 `gps.db`，WAL 模式；7 日原始點約 2,000 筆，整庫長期在數 MB 級。

---

## 6. 手機端設定（Android）

### 6.1 首選：OwnTracks（免費、開源，Play Store／F-Droid）

1. 安裝 **OwnTracks**，開 app → 右上選單 → **Preferences**。
2. **Connection**
   - `Mode`：**HTTP**（不是 MQTT）。
   - `URL`：`https://gps.example.com/pub`
3. **Identification**
   - `Username`：`gpsphone`
   - `Password`：`/srv/gps/.env` 內的 `INGEST_PASS`（用 Bitwarden 之類貼上，不要經聊天傳）
   - `Device ID`：`phone1`（之後查詢用這個名）
   - `Tracker ID`：`er`（兩個字元）
4. **Advanced**（省電與精度）
   - `locatorInterval`：**300** 秒
   - `locatorDisplacement`：**100** 米
   - `locatorPriority`：**2**（Balanced）
   - 關閉 `Remote configuration`（HTTP 模式本來不支援遠端改間隔，開著只會誤導）
5. 系統權限
   - 位置：**永遠允許**（含背景）。
   - 電池：**不受限制**（Samsung／小米／OPPO 要額外關「省電策略」「自啟動管理」）。
   - 允許開機自動啟動。
6. 按 app 內「Send location now」立即驗證（見第 7 節）。

### 6.2 備選：GPSLogger for Android（免費、開源）

若 OwnTracks 在省電上被殺，可換 GPSLogger：Logging → 上傳 → 「Custom URL」，URL 填
`https://gpsphone:INGEST_PASS@gps.example.com/pub?d=phone1`，body 用
`{"_type":"location","lat":%lat,"lon":%lon,"tst":%timestamp,"acc":%acc,"batt":%battery}`，
method POST、Content-Type `application/json`。GPSLogger 有離線排隊。

### 6.3 備選：MacroDroid／Tasker（**未實測，不建議首選**）

觸發「每 5 分鐘」→ 動作「HTTP Request」POST 到同一個 `/pub`，用 Basic auth，body：

```json
{"_type":"location","lat":[latitude],"lon":[longitude],"tst":[javascript:Math.floor(Date.now()/1000)],"acc":[accuracy]}
```

MacroDroid 的 `tst` 要用 JavaScriptlet 產生 epoch 秒；離線排隊要自己用變數＋陣列模擬，較易出錯。只在 OwnTracks 與 GPSLogger 都不合用時才用。

---

## 7. 驗證方法

### 7.1 伺服器端（Pi 4B，最快）

```sh
curl -s http://127.0.0.1:8095/healthz
sudo docker exec gps-api python3 -c "
import sqlite3;c=sqlite3.connect('/data/gps.db')
print(c.execute('select count(*),max(tst) from raw_points').fetchone())"
```

### 7.2 從外部模擬一筆上報（用你自己的 token）

```sh
curl -s -u gpsphone:$INGEST_PASS -H 'Content-Type: application/json' \
  -X POST https://gps.example.com/pub \
  -d '{"_type":"location","lat":22.3,"lon":114.17,"tst":'"$(date +%s)"',"acc":15,"batt":80,"conn":"w"}' \
  -H 'X-Limit-D: phone1'
```

預期：HTTP 200，body `[]`。之後查詢：

```sh
curl -s -H "Authorization: Bearer $TOKEN_FULL" \
  'https://gps.example.com/api/last?device=phone1'
```

預期：`{"found": true, "tst": ..., "stale": false, "hkt": "..."}`。

### 7.3 手機端

OwnTracks → 選單 → 「Send location now」。10 秒內 `/api/last` 的 `tst` 應變成當前時間、`age_seconds` < 60。
若失敗：先看 app 內 Log（Preferences → Log），再看 `docker compose logs gps-api`（只會見 `POST /pub` 與狀態碼）。

### 7.4 全套自測（不碰生產 DB）

```sh
cd shared/gps-docker && python3 selftest.py
```

本機已跑過，22 項全 PASS：健康檢查、未認證拒收、空 body、兩點上報、去重、token 缺失／錯誤、`/api/last`、`recent` 越權封鎖、聚合命名、聚合不含座標、recent 只取 2 小時、指令排隊與只送一次、時鐘偏差拒收、admin 權限、清理後保留聚合。

---

## 8. 查詢方式

所有查詢：`Authorization: Bearer <token>`。`full` = Mine／蹤跡bot；`recent` = 九巴bot。

| 端點 | 用途 | 權限 |
|---|---|---|
| `GET /api/last?device=phone1` | 最新一點（含 `age_seconds`、`stale`、HKT 時間） | full、recent |
| `GET /api/today?device=phone1` | 今日摘要＋停留時間軸 | full、recent |
| `GET /api/day?day=2026-11-12&device=phone1` | 指定日（recent 僅今日） | full、recent |
| `GET /api/points?from=&to=&device=` | 原始點（epoch 秒） | full |
| `GET /api/days?from=&to=` | 每日匯總列表 | full |
| `GET /api/stats` | 庫存統計、保留期設定 | full |
| `POST /api/admin/cmd` | 排 `reportLocation` 即時補點 | full |
| `POST /api/admin/aggregate` | 手動補算某日 | full |
| `POST /api/admin/purge` | 刪原始／聚合 | full |
| `GET /healthz` | 健康檢查（無需 token） | 公開 |

### 8.1 實例：今日去過邊

```sh
curl -s -H "Authorization: Bearer $TOKEN_FULL" https://gps.example.com/api/today | jq
```

回傳（實測樣本，已驗證不含座標）：

```json
{"day":"2026-11-13",
 "summary":{"points":288,"distance_km":31.4,"stay_minutes":640},
 "stays":[{"seq":1,"place_name":"家","source":"geofence","arrive_hkt":"09:05","leave_hkt":"09:41","minutes":36},
          {"seq":2,"place_name":"公司","source":"geofence","arrive_hkt":"10:02","leave_hkt":"18:33","minutes":511}]}
```

### 8.2 實例：上週在某地點停留多久

```sh
curl -s -H "Authorization: Bearer $TOKEN_FULL" \
 'https://gps.example.com/api/day?day=2026-11-06' \
 | jq '[.stays[] | select(.place_name=="大欖隧道轉車站") | {hkt:.arrive_hkt, minutes}]'
```

或直接查本地庫（在 Pi 4B 上）：

```sh
sudo docker exec gps-api python3 -c "
import sqlite3;c=sqlite3.connect('/data/gps.db')
for r in c.execute(\"select day,place_name,minutes from stays where place_name='公司' and day between '2026-11-06' and '2026-11-12' order by day\"): print(r)"
```

### 8.3 九巴bot 的標準呼叫（recent token）

```sh
curl -s -H "Authorization: Bearer $TOKEN_RECENT" \
 'https://gps.example.com/api/last?device=phone1'
```

判讀紀律：`stale: true`（> 30 分鐘）或 `found: false` 時，一律答「位置未知」，不得用舊位置冒充現時位置；需要即時點就打 `POST /api/admin/cmd`（九巴bot 用 `full`? 否——指令端點屬 full，九巴bot 只讀，需要補點時經 Mine 或由用戶手動 Send location now）。

> 若日後想讓九巴bot 自己排 `reportLocation`，在那個 token 的角色判斷加一條例外即可；現時按拍板結果從嚴。

---

## 9. 私隱、保留期、刪除與存取控制

| 項目 | 設定 |
|---|---|
| 原始點保留 | **7 日**（`RAW_RETENTION_DAYS=7`，每日 03:xx HKT 自動刪） |
| 日聚合 | **永久**（`KEEP_AGGREGATES=1`）；只含地名、到達／離開、分鐘，**無座標** |
| 長期層去識別 | 未登記地點在聚合時可選用 Nominatim 反向地理編碼成「地名」文字（`GEOCODE=1`），座標不入庫；關閉時記為「未命名地點」 |
| 傳輸 | 全程 HTTPS（Cloudflare）；`/pub` 用 HTTP Basic ＋ 長隨機密碼 |
| 儲存位置 | 只存 Pi 4B `/srv/gps/data/gps.db`，不複製到共享區、不寫入 bot 記憶、不寫入巡檢報告 |
| 讀取分級 | `full`（全量）／`recent`（最近 2 小時原始點＋今日摘要） |
| 撤銷 | 換 `TOKEN_*` 再 `docker compose up -d`，舊 token 即刻失效；`INGEST_PASS` 同理 |
| 刪除機制 | `POST /api/admin/purge {"scope":"raw"}`／`{"scope":"aggregates","before":"2026-11-01"}`／`{"scope":"both"}`；`audit` 表記錄每次清理 |
| 日誌 | 只印方法與路徑；不記 payload、不記認證標頭 |
| 對 bot 的紀律 | 位置資料只用於回答當次問題與明示的軌跡分析；不得批量匯出到共享記憶或報告 |

**仍未做、需要你日後決定的一項**：若你要求「一鍵全清」，我可以加一個 `POST /api/admin/wipe`（連 `days`／`stays`／`audit` 一併清空），現時故意不做，避免誤觸一鍵毀掉永久摘要。

---

## 10. 成本估算

| 項目 | 費用 |
|---|---|
| Cloudflare | 0（現有 zone；只補 `gps` 這條的回源，免費方案足夠；rate limiting 免費額度 1 條規則） |
| 硬件 | 0（用現有 Pi 4B） |
| 容器資源 | **實測**（2026-11-20，本機 Linux x86 容器，非 Pi 4B）：起機 RSS 22.3 MB；載入 6 條路線共 282 條站序後 27.6 MB；單次推斷後 31 MB。空閒 30 秒 CPU 佔用 0.03%（等於 0）。以 Pi 4B 較慢約 2–3 倍估算，RSS 仍然 < 40 MB。**一個 Python 容器，無第三方套件** |
| 磁碟 | 7 日原始點約 2,000 筆 ≈ 0.3 MB；`stays` 每年約 1,000 行 ≈ 0.2 MB。5 年 < 10 MB |
| 維護人力 | 每月一次健康檢查（1 分鐘），憑證輪換（每年或不定期） |

### 10.1 效能實測（2026-11-20，回答「Python 會不會吃爆樹莓派」）

量法：`/home/rakazo/tmp/gpswork/bench.py`、`bench2.py`（本機真跑，非估算）。

| 場景 | 實測 | 換算到 Pi 4B（保守 ×3） |
|---|---|---|
| 空閒（只有 scheduler 線程睡著） | RSS 22.3 MB、CPU 0.033% | 仍近乎 0 |
| 單筆上報 `POST /pub` | 0.57 ms CPU | 約 1–2 ms |
| 手機每 30 秒一點（2,880 筆／日） | 每日 1.6 秒 CPU | 每日 < 5 秒（單核 0.006%） |
| 一次 `/api/infer`（6 條路線、282 站序、40 個點） | 47 ms 牆鐘、4.5 ms CPU | 約 100–150 ms 牆鐘 |
| 一次 `sync-routes`（6 條路線，含外網抓取） | 1.9 秒 CPU、6–14 秒牆鐘 | 每週一次，可忽略 |
| 記憶體成長 | 40 個點後 +3.5 MB；推斷結果不常駐 | < 40 MB |

結論：**CPU 與記憶體都不是瓶頸**，Pi 4B 跑 Grafana＋VictoriaMetrics 之餘仍有大量餘裕。
真正的成本在 SD 卡寫入（見下），而那也是每日數 MB 級。

### 10.2 SD 卡寫入（Pi 4B 唯一需要留意的項）

- 2,880 筆／日，每筆一次 SQLite 交易（WAL 模式），約 3–8 KB → **每日約 10–20 MB 寫入**。
- 對比：Grafana／VictoriaMetrics 本身的寫入量高於此。若日後要再省，可改為每 5 筆或每 2 分鐘批量 commit，
  但現階段不值得增加複雜度。
- 建議（非必需）：DB 目錄放 USB SSD 而非 SD 卡；若只有 SD 卡，`PRAGMA synchronous=NORMAL` 已是預設，無需改。

---

## 11. 維護要點與故障排查

| 症狀 | 先查 | 常見原因 |
|---|---|---|
| 手機沒上報 | `docker compose logs gps-api` 有無 `POST /pub` | ① 手機背景被殺 ② URL／密碼錯 ③ Cloudflare hostname 未生效 |
| 有 `POST /pub` 但 401 | `.env` 的 `INGEST_PASS` 與 app 內密碼是否一致 | 改 .env 後未 `up -d` 重建容器 |
| 一直 404 | Traefik router 的 `Host()` 與 Cloudflare hostname 是否一致 | 回源指錯／Traefik 未載入動態設定 |
| 外部回 444 | 先在 Pi 4B 用 `curl --resolve` 測 Traefik | CF 有 DNS 記錄但回源缺失（2026-11-18 探測即此狀態） |
| `stale: true` | 手機電量／Doze／廠商省電 | 關省電、鎖定背景 |
| 日聚合空 | `POST /api/admin/aggregate {"day":"..."}` 補算 | 當日 03:xx 容器重啟錯過；或當日所有停留 < 5 分鐘 |
| 地點顯示「未命名」 | `places.json` 未登記該點 | 補一條 place 後重算該日 |
| 磁碟增長異常 | `du -sh /srv/gps/data` | 清理 job 沒跑（檢查容器時區與 `RAW_RETENTION_DAYS`） |

每月例行：
1. `curl -s https://gps.example.com/healthz`
2. `GET /api/stats` 看 `raw_points` 是否維持在 7 日對應量級（約 2,000±500）
3. 備份：`sqlite3 /srv/gps/data/gps.db ".backup /path/to/nas/gps-$(date +%F).db"`（每週一次足夠）
4. 每年或不定期輪換三個密鑰

---

## 12. 未實測清單（不要當作已知事實）

1. **Cloudflare 現有 ingress 全清單**未量度（無 CF API token）。已量度的是對外行為：`gps`／`www`／`vikunja`／`nas` 回 444、`grafana`／`dsh` 302 到 Access、`example.com` 200（2026-11-18 21:30）。故「`gps` 缺回源」成立，但屬 Tunnel 還是 proxied A 未量度；proxied A ＋ Origin Rule 8443 的可行性未實測。
2. OwnTracks **HTTP 模式回傳 `cmd: reportLocation` 在你的 Android 版本是否真的即時補點**：官方文件列出支援，但我未在真機驗證（草案 R1 同類風險）。
3. Doze 下的實際延遲分佈（5 分鐘間隔的 `tst` 抖動）。
4. 廠商省電（小米／華為／OPPO）是否會殺 app。
5. 每日耗電（估算 2–4%，未實測）。
6. 室內精度與斷點比例。
7. Nominatim 反向地理編碼的中文地名質素（`GEOCODE=1` 才啟用，預設關）。
8. Traefik 的 entrypoint 名、provider（file／docker）與 network 名：未量度，要你實查後照改 `traefik-gps.yml`。
9. Pi 4B 內部是否另有非 443 的 nginx：未量度（不影響本設計）。

以上任何一項未實測前，不得寫成「已支援」或「正常運作」。

---

## 13. 回退與遷移

| 情況 | 做法 |
|---|---|
| 完全放棄 | 刪 Cloudflare 上 `gps` 的回源（DNS 記錄原本就在，也可一併刪）、`docker compose down -v`、刪 `/srv/gps`（DB 一併刪） |
| 換主機（NAS，仍在 10.x） | 抄 `/srv/gps` 三個檔案 ＋ `gps.db`，改 Cloudflare 回源與 Traefik router；程式無主機依賴。**不要搬去 Pi 5 或 VLAN 30**（見 §1 反面推論） |
| 換回 Cloudflare Worker ＋ D1 | `app.py` 的 handler 邏輯可直接改寫成 Worker `fetch`；schema 幾乎照搬（SQLite → D1）；手機 URL 不用改 |
| 想恢復遠端改間隔 | 另加 MQTT 通道（需新端口，須你另行批准），或改用 GPSLogger 的設定檔遠端更新 |

---

## 14. 檔案清單

| 檔案 | 用途 |
|---|---|
| `shared/gps-docker/app.py` | 服務本體（上報、查詢、聚合、清理） |
| `shared/gps-docker/docker-compose.yml` | 容器定義（loopback 綁定、healthcheck、日誌上限） |
| `shared/gps-docker/traefik-gps.yml` | Traefik 路由（file provider；docker labels 版按檔內註解改寫） |
| `shared/gps-docker/places.example.json` | 已命名地點範本（要改成真實座標） |
| `shared/gps-docker/selftest.py` | 34 項端到端自測（含路線推斷），不碰生產 DB |
| `shared/gps-docker/deploy/make_deploy.py` | 生成部署腳本（內嵌 app.py，可重複執行） |
| `shared/gps-docker/deploy/gps-deploy-pi4.sh` | 一鍵部署：放檔／密鑰／起容器／自動接 Traefik／驗 healthz，支援 `--dry-run` |
| `shared/gps-docker/patch_infer.py`／`patch_selftest.py` | 把推斷功能與其自測加進 app.py／selftest.py 的補丁（已執行，保留供追溯） |
| `shared/gps-上報-設置手冊.md` | 本文件 |
"""


NEW_SECTIONS = r"""

---

## 15. 路線推斷：由座標判斷「在哪條線、哪個方向、走到第幾站」

這一節回應 owner 原話「讓九巴bot 追蹤到我在哪個巴士」。**上報只是前半，後半是推斷**，此節補上。

### 15.1 能力邊界（先講不能做的）

| 想要 | 可否做到 | 原因 |
|---|---|---|
| 哪條路線、哪個方向（O/I）、走到第幾站、下一站 | **可以** | 用 KMB 開放數據的站序折線做地圖匹配 |
| 是否正在車上（moving）還是停在站／路邊 | **可以** | 軌跡位移與段速 |
| 同一走廊的多條路線（如 968／968X／P968 在西隧重疊） | **只能列出候選並標明歧義** | 幾何上無法區分，須用 ETA 交叉核對 |
| 車牌／班次編號 | **做不到** | KMB 開放數據沒有車輛 GPS；任何聲稱認得車牌的做法都是猜 |

### 15.2 算法（app.py 內 `infer_candidates()`，純標準庫）

1. **路線表**：`route_stops` 由 `/api/admin/sync-routes` 從 `data.etabus.gov.hk` 建立（站序、站名、座標）。只同步你關心的路線（環境變數 `INFER_ROUTES`），避免每日幾千次請求。
2. **候選篩選**：對每個 (route, bound, service_type)，逐點算「最近站距」與「到站間線段距離」，取較小者；平均 > `INFER_MAX_OFF_M`（預設 300 m）即剔除。
3. **四項計分**（權重固定，可讀程式碼核對）：
   - 沿線距離 `0.45`：越貼線越高分。
   - 覆蓋率 `0.20`：有多少比例的點在 150 m 內。
   - 方向一致 `0.20`：用戶兩點位移方位 vs 該站前後 2 站的走線方位，≤45° 全分、≤90° 半分。
   - 站序單調 `0.15`：沿線索引是否遞增（倒退即扣分，可排除對向）。
4. **是否在車上**：位移 ≥ `INFER_MIN_SPAN_M`（400 m）且最高段速 ≥ `INFER_MIN_SPEED_KMH`（12 km/h）才算 `moving`；否則 `state=stationary`，信心強制 `low`——寧可說「在該線沿線」也不說「在車上」。
5. **信心**：`high` 需 分數 ≥0.80 ＋ 覆蓋 ≥0.8 ＋ 單調 ≥0.8 ＋ 與次名差距 ≥0.08；否則 `medium`（分數 ≥0.62）；同走廊差距 <0.05 一律降為 `medium` 並在 `reasons` 講明。
6. **交給九巴bot 的把手**：候選內附 `eta_probe`（未來 3 個站的 seq／stop_id／站名），九巴bot 自己用它的 ETA 技能核對「你 5 分鐘後到的那班車，與 ETA 是否吻合」，吻合才升級為「確定在 968X 上」。

### 15.3 實測（2026-11-20 08:0x，本機 + 真實 KMB 數據）

| 測試 | 結果 |
|---|---|
| `POST /api/admin/sync-routes` 同步 968X／968／978／P968 | 200，186 個站序點、10 個 route-bound-service 序列、耗時 12.5 秒 |
| 沿真實 968X（元朗德業街 → 新威園）15 站模擬行走 8 點、24 km/h、橫向偏移 15 m | top1 **968X/O**、`confidence=high`、沿線距 11 m、方向一致 93%、站序 1→8 |
| 沿真實 978 模擬行走 | top1 **978/O**、`high`、沿線距 9 m |
| 合成同走廊第二條線（座標完全重疊） | 降為 `medium`，`reasons` 寫明「與 T1B/O 分數差 <0.05（同走廊），須用 ETA 交叉核對」 |
| 靜止在同一個站 4 點 | `state=stationary`、`confidence=low`（不會誤報「在車上」） |
| 遠離所有已同步路線 | `state=off_route`、候選為空 |
| 只有 1 個點 | `state=unknown`，不亂猜 |

### 15.4 端點

```
GET /api/infer?window_min=45&device=phone&ts=<epoch>
Authorization: Bearer <TOKEN_RECENT 或 TOKEN_FULL>
```

- `window_min` 預設 45，上限夾在 `RECENT_WINDOW_MIN`（120）。5 分鐘一報的話，45 分鐘窗有 6–9 點，足夠判方向。
- `recent` token 只能用最近 2 小時的點（與既有權限一致），且回傳會附加這批點本身；`full` 可指定 `ts` 回溯。
- 回傳重點：`state`（moving／stationary／off_route／unknown）、`speed_kmh`、`span_m`、`place`、`candidates[0]`（route／bound／score／confidence／at_stop／next_stop／eta_probe）、`reasons`（中文理由，可直接給人看）、`disclaimer`。

```
GET /api/route-stops?route=968X&bound=O         # 除錯用：看已同步站序
POST /api/admin/sync-routes {"routes":["968X","968","978","P968"]}   # 需 full token
```

### 15.5 維護

- 路線改動（加站、改道）不會通知你，所以要定期重跑 sync。建議每週一次（手動或加一條 routine 打 admin 端點）。
- `route_stops` 只存公開站序資料，**不含任何個人位置**，可安全備份或重建。
- 新增路線只改 `INFER_ROUTES` 再 sync，不需改程式碼。

---

## 16. 九巴bot 讀取接口（設計；本次不改九巴bot 指令）

owner 2026-11-18 兩次講「不碰九巴bot」。故此節只寫「它應該怎麼讀」，實作與改指令由 owner 批准後進行。

### 16.1 它應該問的三個問題與對應呼叫

| 問題 | 呼叫 | 說明 |
|---|---|---|
| 你而家喺邊？ | `GET /api/last` | 最近一點＋`age_seconds`／`stale`；超過 30 分鐘 `stale=true`，不要當即時 |
| 你而家係唔係喺車上、邊條線？ | `GET /api/infer?window_min=45` | 取 `state` 與 `candidates[0]`；`confidence` 非 high 就照實講「可能係 X 或 Y」 |
| 你今日去過邊？ | `GET /api/today` | 只有地名與時間，無座標 |

### 16.2 建議的回報句式（避免過度確定）

- `high`：你在 968X（往新威園方向），約第 8 站，下一站形點 I。
- `medium`（同走廊）：你可能在 968X 或 968／P968（西隧走廊重疊），你 5 分鐘後會到形點 I，ETA 顯示…
- `stationary`：你現在在 968X 沿線的形點 I 附近，但未移動，未確定是否上車。
- `off_route`：你不在任何已同步路線附近（只同步了 968X／968／978／P968，其他線未收錄）。

### 16.3 權限與紀律

- 只發 `TOKEN_RECENT`。它拿不到歷史軌跡（`/api/points` 403、`/api/day` 只回今日＋最近 2 小時）。
- **禁止**把 token 寫進指令正文或聊天；放 bot 的環境變數或密鑰管理。
- 推斷結果一律附 `disclaimer` 精神：不得把 `medium` 講成確定，不得虛構車牌。

### 16.4 要改九巴bot 時的最小改動

只需在它的指令加一段「查位置」規則（呼叫哪兩個端點、怎麼讀 confidence、怎麼講），不碰它既有的 ETA／月票邏輯。這段要 owner 明確批准才動。

### 16.5 上報格式：與 OwnTracks 原生 schema 的對照（實測 app.py:643）

手機端用 OwnTracks（Android）HTTP 模式，`/pub` 直接吃它的原生 payload，**不做任何格式轉換**：

| OwnTracks 欄位 | app.py 是否使用 | 落點 |
|---|---|---|
| `_type` | 必須等於 `location`，否則整包忽略 | — |
| `lat`／`lon` | 必須存在（float） | `raw_points.lat/lon` |
| `tst` | 必須存在（UTC epoch 秒）；與伺服器差 >7 日即拒收並寫 `audit.reject_skew` | `raw_points.tst` |
| `acc`（米） | 選填 | `raw_points.acc`；>120 米的點在聚合時不計停留 |
| `vel`（km/h） | 選填 | `raw_points.vel`（推斷優先用自行計算的速度，缺點才用此值） |
| `batt`／`conn` | 選填 | `raw_points.batt/conn` |
| `tid` | 選填（取前 4 字） | `raw_points.tid`，用作去重輔助 |
| 其他（`alt`、`m`、`bs`、`cog`…） | 忽略，不報錯 | — |

裝置辨識：`X-Limit-D` header → `?d=` 查詢參數 → 預設 `phone`（`app.py:644`）。
回應：固定 200，body 是 JSON 陣列（OwnTracks 認得的 remote command 佇列；無指令時為 `[]`）。
空 body（OwnTracks 刪 friend 時會發）回 200 `[]`，不當錯誤。

**注意**：不做 OwnTracks 的 MQTT 模式（要額外 8883 端口，owner 已定不開新端口），只用 HTTP 模式。

### 16.6 觸發方式：為何是「九巴bot 自取」而不是「上報後推送」

實測與機制限制：

1. Rakazo 的 bot 只會被三種東西喚醒：用戶訊息、自己的 routine、其他 bot 的 `message_bot`。**沒有「外部 HTTP 打進來就叫醒 bot」的入口。**
2. 所以「上報後即時推送給九巴bot」在現有平台上做不到——除非 app.py 反過來呼叫 Rakazo API，那需要平台憑證落在 Pi 4B 上，等於把 bot 控制權放到家居機器，違反「家居零暴露」原則，不採用。
3. 結論：**輪詢**。九巴bot 用一條 routine 定期 `GET /api/state`，看到「我在哪架車」變了才說話，其餘完全靜默。

成本控制（重要，否則每 10 分鐘一次 LLM run 會燒額度）：

| 做法 | 每小時 LLM run | 說明 |
|---|---|---|
| 每 10 分鐘 poll，每次都講話 | 6 | 最貴，且大部分時候沒話可說 |
| 每 10 分鐘 poll，帶 `since` 只在不變時靜默 | 6 次「查」但 0 次輸出 | 仍要付模型讀一次的空轉成本 |
| **建議：Rakazo `watch`（kind=http）盯 `/api/state`** | 0 | 平台靜默檢查不用模型；只在 `change` 變動時喚醒一次，那一次才花模型 |

`watch` 設定要點（由 Mine 決定是否落實到九巴bot，我不改它指令）：

```
kind: http
url:  https://gps.example.com/api/state?device=phone
everyMinutes: 5
track: ["change"]        # 只認 change 欄位，其他欄位變動不喚醒
```

但 `watch` 的 http 模式**不能帶 header**（平台限制：no headers, auth, body），而 `/api/state` 需要 Bearer。
解法二選一（要 owner 拍板）：

- **A**：加 `?t=<recent token>` 查詢參數認證（token 會出現在 URL，可能落入 CF 日誌）→ 需要新增一小段驗證程式碼。
- **B**：在 Pi 4B 內網另開一個唯讀端點（不經 Cloudflare），但 watch 是平台端跑，打不到 10.x，不可行。
- **C**：不用 watch，改用九巴bot 自己的 routine（每 5–10 分鐘，只在通勤時段 07:30–09:30、17:30–20:30），run 內先 `/api/state` 再決定講不講；空轉成本由額度管制決定。

我建議 **A**（配合短有效期 token）或 **C**（配合時段收窄），由 Mine 與 owner 選。不做 A 之前，`/api/state` 的 `since` 靜默輪詢已足夠讓 C 的每次 run 極短。

### 16.7 `/api/state`：為輪詢而設的極簡端點（2026-11-20 新增，自測 23–26）

```
GET /api/state?device=phone[&window_min=45][&since=<上次的 change>]
Authorization: Bearer <TOKEN_RECENT 或 TOKEN_FULL>
```

回應（在車上時）：

```json
{"ts":1795170000,"change":"moving|968X|O","state":"moving","speed_kmh":42.1,
 "age_seconds":18,"stale":false,"route":"968X","bound":"O","confidence":"high",
 "at_stop":"欖口村","next_stop":"大欖隧道轉車站","seq_from":5,"seq_to":6,
 "place":null,"alts":[],"reasons":["..."],"hkt":"2026-11-20 18:40:00"}
```

- `change` 只由 `(state, route, bound)` 組成：**上車／下車／轉線才變**，逐個站不會變。這是刻意的，否則每兩分鐘喚醒一次。
- 未確定或未移動時 `route`／`bound` 為 `null`，`change` 為 `stationary|-|-` 或 `off_route|-|-`。
- `since` 與當前 `change` 相同 → 回 `{"unchanged":true,"change":"...","ts":...}`（<120 bytes），適合靜默輪詢。
- 不支援車牌／班次：`disclaimer` 欄位固定說明，`alts` 列出其他可能路線（同走廊）。

### 16.8 要改九巴bot 時的最小改動

只需在它的指令加一段「查位置」規則（呼叫哪個端點、怎麼讀 change 與 confidence、怎麼講、何時閉嘴），不碰它既有的 ETA／月票邏輯。這段要 owner 明確批准才動。

**建議加進它指令的四句（草案，待 owner 批准）**：

1. 通勤時段先打 `/api/state`；`unchanged` 或 `stale=true` 就閉嘴。
2. `change` 與上次相同 → 不重複講。
3. 只在 `change` 首次出現時講一句：路線＋方向＋下一站；`confidence=medium` 要講「可能係」。
4. 永不講車牌、班次、座位。

---

## 17. 部署步驟（給 owner，取代此前的零散版本）

前置：Pi 4B 有 docker ＋ docker compose ＋ curl。**不需要 SSH 以外的任何新服務**。

```bash
# 1. 把 deploy/gps-deploy-pi4.sh 傳到 Pi 4B（scp 或貼上），然後：
sudo ./gps-deploy-pi4.sh --dry-run      # 先看它偵測到什麼（不寫任何檔案）
sudo ./gps-deploy-pi4.sh                # 真正部署

# 2. 建路線表（推斷用；只需 full token）
curl -sS -X POST -H 'Authorization: Bearer <TOKEN_FULL>' -H 'Content-Type: application/json' \
  -d '{"routes":["968X","968","978","P968"]}' http://127.0.0.1:8095/api/admin/sync-routes

# 3. Cloudflare：為 gps.example.com 補回源（Tunnel 或 proxied A ＋ Origin Rule 8443，二選一）

# 4. 手機 OwnTracks：URL https://gps.example.com/pub、Basic gpsphone／INGEST_PASS、5 分鐘／100 米

# 5. 驗證
curl -sS https://gps.example.com/healthz
```

腳本會自動做的事（不用你判斷）：找出 Traefik 容器、讀它的 docker network、找出 dynamic 目錄、從它的啟動參數取 entryPoint 名，寫 `gps.yml`，並寫 `docker-compose.override.yml` 讓容器重啟後仍在同一 network。**不會 restart Traefik**（file provider 自動載入），不動其他容器。

### 驗收（RAK-12 版）

| # | 驗收 | 判定 |
|---|---|---|
| 1 | `curl https://gps.example.com/healthz` | 回 `{"ok": true, ...}`（不是 `/health`，app 只實作 `/healthz`） |
| 2 | 行走中 `/api/last`（recent token） | `age_seconds` < 600，`stale=false` |
| 3 | `/api/infer`（recent token） | `state=moving`、top1 路線與你實際乘搭相符，或 `reasons` 講明同走廊歧義 |
| 4 | 九巴bot（若已改） | 收到一句「你在 968X（往新威園方向）」等結論，並附依據；`medium` 時不得說成確定 |
| 5 | 私隱 | `/api/points` 用 recent token 回 403；`stays` 表無座標欄位 |

---

## 18. 本版沿革（2026-11-20）

1. **補上後半段**：新增路線推斷（§15）。此前只有上報與查詢，回答不到「在哪班巴士」。
2. **部署腳本重做**：`deploy/make_deploy.py` 生成，內嵌 app.py 並在 Pi 4B 上自動偵測 Traefik（network／dynamic 目錄／entryPoint），取代原本要你手查三項的版本。舊版（40,019 bytes，長遠規劃 bot 2026-11-20 01:14 生成）仍保存在 `deploy/gps-deploy-pi4.zip` 內，可解壓取回。
3. **自測由 22 項增至 34 項**：新增 12 項推斷自測（路線命中、moving／stationary 判定、同走廊降信心、off_route、無路線表 503、window 夾限、eta_probe 存在、無車牌欄位等）。
4. **不動九巴bot**：§16 只寫接口設計，改指令要 owner 批准。
"""


def build():
    parts = [HEAD, read("app.py"), TAIL, read("docker-compose.yml"), MID_PLACES,
             read("places.example.json"), MID_TRAEFIK, read("traefik-gps.yml"), MID_SCHEMA,
             NEW_SECTIONS]
    text = "".join(parts)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote", OUT, len(text), "bytes")


if __name__ == "__main__":
    build()
