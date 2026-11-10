#!/bin/zsh
# 十神版固定端口启动器（P6 发布后使用）
# 默认 8737；可用 BAZI_PORT 覆盖以便在测试端口演练。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${BAZI_PORT:-8737}"

export PYTHONPATH="${PYTHONPATH:-/Users/zhangxingjin/.cache/stock-analyzer/quality-py311-20261012/lib/python3.11/site-packages}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/pyc_ui}"

PY=""
for c in /Users/zhangxingjin/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
  if [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "未找到 python3。请先安装 Xcode Command Line Tools（xcode-select --install）。"
  read -r "?按回车键关闭本窗口..."
  exit 1
fi

echo "十神版启动：$DIR/app/server.py --port $PORT"
cd "$DIR/app" && exec "$PY" -u server.py --port "$PORT" --no-browser
