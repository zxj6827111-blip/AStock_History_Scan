import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

app_dir = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(app_dir))

import api
import bjtime

def test_current_window():
    print("============================================================")
    print("开始验收：根据系统时间（北京时间）推算当前旬与周功能")
    print("============================================================")

    # 1. 验证时区转换
    now_bjt = bjtime.now_bjt()
    print(f"  当前北京时间：{now_bjt.isoformat()}")
    assert now_bjt.tzinfo is not None, "必须为 aware datetime"
    assert now_bjt.utcoffset() == timedelta(hours=8), "必须为固定 UTC+8"

    # 2. 调用 api.calendar_now
    con = api.connect("ashare")
    cal = api.calendar_now(con)

    assert "current_window" in cal, "返回数据必须包含 current_window"
    cur_win = cal["current_window"]
    assert cur_win is not None, "当前进行中的月份必须有窗位数据"

    print(f"  当前旬：xun_no={cur_win['xun_no']}, xun_name={cur_win['xun_name']}")
    print(f"  当前周：week_no={cur_win['week_no']}, week_name={cur_win['week_name']}")
    print(f"  交节起流逝天数：{cur_win['day_offset']} 天")
    print(f"  当前周剩余：{cur_win['week_left_str']}")
    print(f"  当前旬剩余：{cur_win['xun_left_str']}")

    assert cur_win["xun_no"] in (1, 2, 3), "xun_no 必须在 1..3 之间"
    assert cur_win["week_no"] in (1, 2, 3, 4, 5, 6), "week_no 必须在 1..6 之间"

    # 3. 验证 6 周列表
    weeks = cur_win["weeks"]
    assert len(weeks) == 6, "必须恰好分为 6 个周"

    cur_count = sum(1 for w in weeks if w["is_current"])
    assert cur_count == 1, f"同时有且仅有 1 个周为当前周，当前为 {cur_count} 个"

    curr_w = next(w for w in weeks if w["is_current"])
    assert curr_w["week_no"] == cur_win["week_no"], "weeks 列表与 week_no 必须一致"

    # 4. 验证 ETF 数据集同样具备窗位输出
    con_etf = api.connect("etf")
    cal_etf = api.calendar_now(con_etf, dataset="etf")
    assert cal_etf["current_window"] is not None
    assert cal_etf["current_window"]["week_no"] == cur_win["week_no"]

    print("============================================================")
    print("PASS: 当前旬/周计算全部断言 100% 通过！")
    print("============================================================")

if __name__ == "__main__":
    test_current_window()
