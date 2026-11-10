#!/opt/homebrew/bin/python3
"""独立常驻的「当月层日更」进程（不依赖页面服务）。

页面服务里也有同样的钩子（server.py → daily_live_job.start），两边靠
data/live_daily.lock 互斥，谁先起来谁跑，不会重复刷。

Mac 重启后本进程会消失（macOS 的 launchd 读不到这块 USB 盘，不能开机自启）；
需要时重新执行本脚本即可，或由下次启动页面服务时的钩子接管。
"""
import signal
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

import daily_live_job  # noqa: E402

if __name__ == "__main__":
    result = daily_live_job.start(root=APP.parent)
    print(f"[{daily_live_job._now().strftime('%F %T')}] standalone daemon: {result}",
          flush=True)
    if not result.get("started"):
        raise SystemExit(1)
    signal.pause()
