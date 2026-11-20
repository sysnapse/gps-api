#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 deploy/gps-deploy-pi4.sh：把 app.py／compose／places／Traefik 路由以 base64 內嵌，
並在腳本內自動偵測 Pi 4B 上的 Traefik（network、dynamic 目錄、entryPoint）。

用法：python3 make_deploy.py   →  寫出 deploy/gps-deploy-pi4.sh（可重複執行）
"""
import base64
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "gps-deploy-pi4.sh")


def b64(name):
    with open(os.path.join(ROOT, name), "rb") as f:
        raw = f.read()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(), len(raw)


APP_B64, APP_SHA, APP_LEN = b64("app.py")
COMPOSE_B64, COMPOSE_SHA, _ = b64("docker-compose.yml")
PLACES_B64, _, _ = b64("places.example.json")

SCRIPT = r'''#!/usr/bin/env bash
# gps-api 部署腳本（Pi 4B）——由 shared/gps-docker/deploy/make_deploy.py 生成，勿手改。
# 生成來源 sha256：app.py __APP_SHA__（__APP_LEN__ bytes）
#
# 用法（在 Pi 4B 上）：
#   sudo ./gps-deploy-pi4.sh --dry-run     # 只偵測與印出計畫，不寫任何檔案
#   sudo ./gps-deploy-pi4.sh               # 真正部署（預設綁 127.0.0.1，配 Traefik）
#   sudo BIND_ADDR=192.168.1.100 ./gps-deploy-pi4.sh   # 綁內網（給 Cloudflare Tunnel 直達用）
#   sudo TRAEFIK_DYNAMIC_DIR=/etc/traefik/dynamic ./gps-deploy-pi4.sh  # 指定 Traefik 動態目錄
#
# 只做五件事：放檔案、生成密鑰（只寫入 .env）、起容器、自動接上 Traefik、驗 /healthz。
# 不動其他容器、不 restart Traefik（file provider 會自動 reload）、不刪任何東西。
set -euo pipefail

S=/srv/gps
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
TRAEFIK_DYNAMIC_DIR="${TRAEFIK_DYNAMIC_DIR:-}"
DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知參數：$a"; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

step "0) 前置檢查"
[ "$(id -u)" = "0" ] || { say "請用 sudo 執行"; exit 1; }
command -v docker >/dev/null || { say "找不到 docker"; exit 1; }
if docker compose version >/dev/null 2>&1; then DC="docker compose"; elif command -v docker-compose >/dev/null; then DC="docker-compose"; else
  say "沒有 docker compose"; exit 1; fi
command -v curl >/dev/null || { say "找不到 curl（用來驗 /healthz）"; exit 1; }
say "   ok：$DC 可用；bind=$BIND_ADDR；dry-run=$DRY"

step "1) 偵測 Traefik（不改它，只讀）"
TRAEFIK_CT=""
TRAEFIK_CT="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i traefik | awk '{print $1}' | head -n1 || true)"
TRAF_NET=""; TRAF_DYN=""; TRAF_EP=""
if [ -n "$TRAEFIK_CT" ]; then
  say "   找到容器：$TRAEFIK_CT"
  TRAF_NET="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$TRAEFIK_CT" | tr ' ' '\n' | head -n1)"
  say "   network：${TRAF_NET:-（未取到）}"
  # dynamic 目錄：找掛載點內有 traefik 動態設定的目錄
  MOUNTS="$(docker inspect -f '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "$TRAEFIK_CT" 2>/dev/null || true)"
  while IFS='|' read -r src dst; do
    [ -n "${src:-}" ] || continue
    case "$dst" in
      *dynamic*|*traefik*)
        if [ -d "$src" ] && ls "$src"/*.y*ml >/dev/null 2>&1; then TRAF_DYN="$src"; break; fi
        ;;
    esac
  done <<EOF
$MOUNTS
EOF
  if [ -z "$TRAF_DYN" ]; then
    # 後備：容器內 /etc/traefik/dynamic 有無對應宿主目錄
    for cand in /etc/traefik/dynamic /opt/traefik/dynamic /srv/traefik/dynamic /mnt/docker/traefik/dynamic; do
      [ -d "$cand" ] && { TRAF_DYN="$cand"; break; }
    done
  fi
  say "   dynamic 目錄：${TRAF_DYN:-（未偵測到，稍後印出指示）}"
  ARGS="$(docker inspect -f '{{json .Args}}' "$TRAEFIK_CT" 2>/dev/null || echo '')"
  if printf '%s' "$ARGS" | grep -q 'websecure'; then TRAF_EP="websecure"; fi
  if [ -z "$TRAF_EP" ]; then
    TRAF_EP="$(printf '%s' "$ARGS" | tr ',' '\n' | sed -n 's/.*entrypoints\.\([a-zA-Z0-9_-]*\)\.address.*/\1/p' | head -n1)"
  fi
  say "   entryPoint：${TRAF_EP:-（未取到，預設用 websecure）}"
  TRAF_EP="${TRAF_EP:-websecure}"
else
  say "   未找到 Traefik 容器；只會部署容器，Traefik 路由稍後手動加"
fi

if [ "$DRY" = "1" ]; then
  step "dry-run 結束"
  say "   將會："
  say "   1. 寫 $S/{app.py,docker-compose.yml,data/places.json,.env}"
  say "   2. $DC up -d（gps-api，bind $BIND_ADDR:8095）"
  [ -n "$TRAF_NET" ] && say "   3. 把 gps-api 接到 docker network：$TRAF_NET"
  [ -n "$TRAF_DYN" ] && say "   4. 寫 Traefik 路由：$TRAF_DYN/gps.yml（entryPoint $TRAF_EP）"
  [ -z "$TRAF_DYN" ] && say "   4. Traefik 路由需手動：把 traefik-gps.yml 放進 dynamic 目錄"
  say "   5. curl http://127.0.0.1:8095/healthz 應回 {\"ok\": true}"
  exit 0
fi

step "2) 放檔案"
mkdir -p "$S/data"
echo "__APP_B64__" | base64 -d > "$S/app.py"
chmod 644 "$S/app.py"
if [ ! -f "$S/data/places.json" ]; then
  echo "__PLACES_B64__" | base64 -d > "$S/data/places.json"
  say "   已放 places.json（示範座標，之後改成真實座標；只影響地點命名）"
else
  say "   places.json 已存在，不改動"
fi
echo "__COMPOSE_B64__" | base64 -d > "$S/docker-compose.yml"
sed -i "s|127.0.0.1:8095:8095|$BIND_ADDR:8095:8095|" "$S/docker-compose.yml"
say "   已放 app.py（sha256 __APP_SHA__）、docker-compose.yml"

step "3) 密鑰"
if [ ! -f "$S/.env" ]; then
  umask 077
  gen() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
  {
    echo "INGEST_PASS=$(gen)"
    echo "TOKEN_FULL=$(gen)"
    echo "TOKEN_RECENT=$(gen)"
  } > "$S/.env"
  chmod 600 "$S/.env"
  say "   已生成 $S/.env（600）。取用：sudo cat $S/.env，不要貼進聊天或 git"
else
  say "   $S/.env 已存在，不覆寫"
fi

step "4) 起容器"
cd "$S"
$DC up -d
sleep 5
$DC ps gps-api || true

step "5) 本機驗證"
OK=0
for i in 1 2 3 4 5 6 7 8; do
  if curl -fsS --max-time 3 "http://127.0.0.1:8095/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 3
done
if [ "$OK" = "1" ]; then say "   OK：gps-api 活著 → $(curl -fsS http://127.0.0.1:8095/healthz)"; else
  say "   失敗：/healthz 無回應。查日誌：cd $S && $DC logs --tail=50 gps-api"; exit 1; fi

step "6) 接上 Traefik"
if [ -n "$TRAF_NET" ] && [ -n "$TRAEFIK_CT" ]; then
  if docker network connect "$TRAF_NET" gps-api 2>/dev/null; then say "   已把 gps-api 接到 network $TRAF_NET"
  else say "   已在 network $TRAF_NET（或連接失敗，稍後手動：docker network connect $TRAF_NET gps-api）"; fi
  # 讓 gps-api 重啟後仍留在該 network：寫進 compose override
  cat > "$S/docker-compose.override.yml" <<EOF2
services:
  gps-api:
    networks:
      - default
      - traefik
networks:
  traefik:
    external: true
    name: $TRAF_NET
EOF2
  say "   已寫 docker-compose.override.yml（重啟後仍保留該 network）"
else
  say "   跳過（未偵測到 Traefik 容器）"
fi

DYN="${TRAEFIK_DYNAMIC_DIR:-$TRAF_DYN}"
if [ -n "$DYN" ] && [ -d "$DYN" ]; then
  if [ -f "$DYN/gps.yml" ]; then
    say "   $DYN/gps.yml 已存在，不覆寫；請自行核對內容"
  else
    HAS_WEB=0
    printf '%s' "${ARGS:-}" | grep -q '"web"' && HAS_WEB=1
    {
      echo "# 由 gps-deploy-pi4.sh 於 $(date -Iseconds) 生成；只新增 gps 這條路由"
      echo "http:"
      echo "  routers:"
      echo "    gps:"
      echo "      rule: \"Host(\`gps.example.com\`)\""
      echo "      entryPoints: [ $TRAF_EP ]"
      echo "      service: gps"
      echo "      tls: {}"
      if [ "$HAS_WEB" = "1" ]; then
        echo "    gps-redirect:"
        echo "      rule: \"Host(\`gps.example.com\`)\""
        echo "      entryPoints: [ web ]"
        echo "      middlewares: [ gps-to-https ]"
        echo "      service: gps"
      fi
      echo "  services:"
      echo "    gps:"
      echo "      loadBalancer:"
      echo "        passHostHeader: true"
      echo "        servers:"
      echo "          - url: \"http://gps-api:8095\""
      if [ "$HAS_WEB" = "1" ]; then
        echo "  middlewares:"
        echo "    gps-to-https:"
        echo "      redirectScheme:"
        echo "        scheme: https"
        echo "        permanent: true"
      fi
    } > "$DYN/gps.yml"
    chmod 644 "$DYN/gps.yml"
    say "   已寫 $DYN/gps.yml（entryPoint $TRAF_EP；file provider 自動載入，不 restart Traefik）"
  fi
else
  say "   未寫 Traefik 路由。手動做法：把 shared/gps-docker/traefik-gps.yml 放進 Traefik 的 dynamic 目錄"
  say "   （docker inspect <traefik 容器> 的 Mounts 可看到宿主目錄），並把 entryPoints 改成實測值"
fi

step "7) 完成，之後要做的三件事（不需要我）"
say "   1. 建路線站序（做「在哪班巴士」推斷用）："
say "      curl -sS -X POST -H 'Authorization: Bearer <TOKEN_FULL>' -H 'Content-Type: application/json' \\"
say "        -d '{\"routes\":[\"968X\",\"968\",\"978\",\"P968\"]}' http://127.0.0.1:8095/api/admin/sync-routes"
say "   2. Cloudflare：為 gps.example.com 補回源（Tunnel 或 proxied A ＋ Origin Rule 8443，見手冊 §2.5）"
say "   3. 手機 OwnTracks：URL https://gps.example.com/pub，Basic 認證用戶 gpsphone ＋ INGEST_PASS"
say ""
say "   驗證：curl -sS https://gps.example.com/healthz"
'''

SCRIPT = (SCRIPT.replace("__APP_B64__", APP_B64)
                .replace("__PLACES_B64__", PLACES_B64)
                .replace("__COMPOSE_B64__", COMPOSE_B64)
                .replace("__APP_SHA__", APP_SHA)
                .replace("__APP_LEN__", str(APP_LEN))
                .replace("__TRAF_EP__", "${TRAF_EP}"))

with open(OUT, "w", encoding="utf-8") as f:
    f.write(SCRIPT)
os.chmod(OUT, 0o755)
print("wrote", OUT, len(SCRIPT), "bytes; app.py sha256", APP_SHA)
