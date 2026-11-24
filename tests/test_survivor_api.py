"""生存者只读 API：历史交易日列表、档位枚举与历史档读取。

跑法：python3 -m unittest -v tests.test_survivor_api（在 tengod 根目录）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(D))

import survivor_api as S  # noqa: E402


class TestSurvivorHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        reports = self.dir / "reports"
        reports.mkdir()
        self._saved = S.SURVIVOR_DIR
        S.SURVIVOR_DIR = self.dir
        for name, asof in (
            ("survivor_2026-11-20_1500_PM.json", "2026-11-20"),
            ("survivor_2026-11-23_1000_INTRADAY.json", "2026-11-23"),
            ("survivor_2026-11-23_1500_PM.json", "2026-11-23"),
            ("survivor_2026-11-24_1030_INTRADAY.json", "2026-11-24"),
        ):
            (reports / name).write_text(
                json.dumps({"asof_date": asof, "rows": []}), encoding="utf-8")
        (reports / "latest.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        S.SURVIVOR_DIR = self._saved
        self._tmp.cleanup()

    def test_dates_desc_with_slot_counts(self):
        out = S.dates({})
        self.assertEqual([d["date"] for d in out["dates"]],
                         ["2026-11-24", "2026-11-23", "2026-11-20"])
        self.assertEqual(out["dates"][1], {"date": "2026-11-23", "slots": 2})

    def test_slots_of_past_day(self):
        out = S.slots({"date": ["2026-11-23"]})
        self.assertEqual([(s["hhmm"], s["session"]) for s in out["slots"]],
                         [("1000", "INTRADAY"), ("1500", "PM")])

    def test_slot_reads_past_report(self):
        rep = S.slot({"date": ["2026-11-23"], "hhmm": ["1500"], "session": ["PM"]})
        self.assertEqual(rep["asof_date"], "2026-11-23")

    def test_missing_past_slot_is_503(self):
        with self.assertRaises(S.SurvivorUnavailable):
            S.slot({"date": ["2026-11-21"], "hhmm": ["1500"], "session": ["PM"]})


if __name__ == "__main__":
    unittest.main()
