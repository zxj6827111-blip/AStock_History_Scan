#!/bin/zsh
# 一键停止：页面服务 + 日更守护（两个都停，文件锁随之释放）
set -u
PORT="${BAZI_PORT:-8737}"

PIDS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)"
if [ -n "$PIDS" ]; then
  echo "停止页面服务（PID $(echo $PIDS)）"
  kill $PIDS 2>/dev/null
else
  echo "页面服务没在跑（端口 $PORT 无监听）。"
fi

DPIDS="$(pgrep -f "live_daily_daemon.py" 2>/dev/null)"
if [ -n "$DPIDS" ]; then
  echo "停止日更守护（PID $(echo $DPIDS)）"
  kill $DPIDS 2>/dev/null
else
  echo "日更守护没在跑。"
fi

sleep 2
echo
echo "检查结果："
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  页面服务：仍在跑（可能还在收尾，等几秒再看）"
else
  echo "  页面服务：已停"
fi
if pgrep -f "live_daily_daemon.py" >/dev/null 2>&1; then
  echo "  日更守护：仍在跑"
else
  echo "  日更守护：已停"
fi
read -r "?按回车键关闭本窗口..."
