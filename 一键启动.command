#!/bin/zsh
# 一键启动：日更守护（后台常驻） + 页面服务（前台，关掉本窗口即停）
# 两个进程共用 data/live_daily.lock：日更只会有一份在跑，先起的那个负责。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/app"
PORT="${BAZI_PORT:-8737}"
PY_UV="/Users/zhangxingjin/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3"

die() { echo "$1"; read -r "?按回车键关闭窗口..."; exit 1; }

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "端口 $PORT 上已经有页面服务在跑；要重启请先运行「一键停止.command」。"
fi

if pgrep -f "live_daily_daemon.py" >/dev/null 2>&1; then
  echo "日更守护已在运行（PID $(pgrep -f live_daily_daemon.py | head -1)），跳过。"
else
  # 日更要 paramiko 连 NAS；brew 升级会把 /opt/homebrew/bin/python3 换到没装 paramiko 的大版本
  # （11-01 换 3.15 后日更就此哑掉），所以逐个探能否 import，不写死路径。
  PY_DAEMON=""
  for c in /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$c" ] && "$c" -c "import paramiko" >/dev/null 2>&1; then PY_DAEMON="$c"; break; fi
  done
  if [ -z "$PY_DAEMON" ]; then
    echo "警告：没有能 import paramiko 的 python，日更守护跳过启动（页面服务照常起）。"
    echo "      补一个：/opt/homebrew/bin/python3.14 -m pip install --user paramiko"
  else
    mkdir -p "$DIR/data"
    cd "$APP"
    nohup "$PY_DAEMON" -u live_daily_daemon.py >> "$DIR/data/live_daily_daemon.out" 2>&1 &
    sleep 1
    if pgrep -f "live_daily_daemon.py" >/dev/null 2>&1; then
      echo "日更守护已启动（PID $(pgrep -f live_daily_daemon.py | head -1)，$PY_DAEMON），运行日志 data/live_daily.log"
    else
      die "日更守护没起来，看 data/live_daily_daemon.out"
    fi
  fi
fi

export PYTHONPATH="${PYTHONPATH:-/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/lib/python3.11/site-packages}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/pyc_ui}"
PY=""
for c in "$PY_UV" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
  if [ -x "$c" ]; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "未找到 python3"

echo "页面服务启动：$APP/server.py --port $PORT"
cd "$APP" && exec "$PY" -u server.py --port "$PORT" --no-browser
