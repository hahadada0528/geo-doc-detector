#!/bin/bash
# Double-click on macOS to start the GEO tool.
cd "$(dirname "$0")" || exit 1

PORT=8765
HOST=127.0.0.1
URL="http://${HOST}:${PORT}"
LOG="data/start_error.log"
mkdir -p data

log() { printf '%s\n' "$*" | tee -a "$LOG"; }

: > "$LOG"
log "start $(date)"
log "cwd=$(pwd)"

find_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
    return 0
  fi
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "未找到 Python。请先安装 Python 3.11 或以上：https://www.python.org/downloads/"
  osascript -e 'display alert "未找到 Python" message "请先安装 Python 3.11 或以上，然后再双击 start_geo.command。"' >/dev/null 2>&1 || true
  exit 1
fi

VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
MAJOR="${VER%%.*}"
MINOR="${VER#*.}"
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]; }; then
  echo "当前 Python 是 $VER，需要 3.11 或以上。"
  echo "请从 https://www.python.org/downloads/ 安装后再运行。"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "第一次运行：正在创建虚拟环境并安装依赖，需要联网，请稍等…"
  "$PY" -m venv .venv || { echo "创建 .venv 失败"; exit 1; }
  PY=".venv/bin/python"
  "$PY" -m pip install -U pip || { echo "升级 pip 失败"; exit 1; }
  "$PY" -m pip install -r requirements.txt || { echo "安装依赖失败，请检查网络后重试"; exit 1; }
fi
log "PYEXE=$PY"

port_open() {
  "$PY" -c "import socket,sys; p=int(sys.argv[1]); s=socket.socket(); s.settimeout(0.6); e=s.connect_ex(('127.0.0.1', p)); s.close(); sys.exit(0 if e==0 else 1)" "$1"
}

# Optional services stay up after this window closes, same as the Windows launcher.
# Hermes Proxy (8645)
if port_open 8645; then
  echo "Hermes Proxy 已在 8645 运行"
  log "hermes_proxy_already_running"
elif command -v hermes >/dev/null 2>&1; then
  echo "正在启动 Hermes Proxy（8645）…"
  nohup hermes proxy start --provider nous --host 127.0.0.1 --port 8645 >>"$LOG" 2>&1 &
  disown || true
  log "hermes_proxy_started"
else
  echo "未找到 hermes 命令，跳过 Hermes Proxy"
  log "hermes_proxy_skipped"
fi

# KB Relay (8080) and Wiki API (8686) — optional
if port_open 8080; then
  echo "知识库中继已在 8080 运行"
  log "kb_relay_already_running"
elif "$PY" resolve_kb_dir.py check relay 2>/dev/null | grep -q OK; then
  echo "正在启动知识库中继（8080）…"
  nohup "$PY" resolve_kb_dir.py relay >>"$LOG" 2>&1 &
  disown || true
  log "kb_relay_started"
else
  echo "未找到知识库 relay_server.py，跳过中继"
  echo "若需要写稿/知识库：把「数据库测试/数据库测试」放到本文件夹的上一级，或设置 GEO_KB_ROOT"
  log "kb_relay_skipped"
fi

if port_open 8686; then
  echo "Wiki API 已在 8686 运行"
  log "wiki_api_already_running"
elif "$PY" resolve_kb_dir.py check wiki 2>/dev/null | grep -q OK; then
  echo "正在启动 Wiki API（8686）…"
  nohup "$PY" resolve_kb_dir.py wiki >>"$LOG" 2>&1 &
  disown || true
  log "wiki_api_started"
else
  echo "未找到知识库 api_server.py，跳过 Wiki API"
  log "wiki_api_skipped"
fi

if port_open "$PORT"; then
  echo "GEO 工具已在运行，正在打开浏览器…"
  log "already_running"
  open "$URL"
  exit 0
fi

echo
echo "============================================"
echo "  GEO 工具:       $URL"
echo "  知识库中继:     http://127.0.0.1:8080"
echo "  Hermes Proxy:   http://127.0.0.1:8645"
echo "  Wiki API:       http://127.0.0.1:8686"
echo "============================================"
echo "关闭这个窗口会停止本次启动的 GEO 工具。"
echo "日志: $LOG"
echo

(sleep 2 && open "$URL") >/dev/null 2>&1 &
log "geo_starting"
"$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
ERR=$?
log "exit_code=$ERR"
if [ "$ERR" -ne 0 ]; then
  echo
  echo "启动失败，代码 $ERR。请查看 $LOG"
fi
exit "$ERR"
