"""首屏规则状态轻量模式：不得为首屏用不到的统计扫描规则明细。

跑法：python3 -m unittest -v tests.test_window_status_lite（在 tengod 根目录）
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import api  # noqa: E402


class _NoQueryConnection:
    def execute(self, *_args):
        raise AssertionError("lite 模式不应执行分类计数或历史跨度扫描")


class TestWindowRulesStatusLite(unittest.TestCase):
    @mock.patch.object(api, "_xw_ready", return_value=True)
    @mock.patch.object(api, "_half_ready", return_value=True)
    def test_lite_only_returns_first_screen_fields(self, _half, _xw):
        out = api.window_rules_status(_NoQueryConnection(), lite=True)
        self.assertEqual(out, {
            "available": True,
            "layers": ["month", "xun", "week", "half"],
            "half_available": True,
        })

    @mock.patch.object(api, "_xw_ready", return_value=True)
    @mock.patch.object(api, "_half_ready", return_value=False)
    def test_lite_hides_half_when_rule_table_is_absent(self, _half, _xw):
        out = api.window_rules_status(_NoQueryConnection(), lite=True)
        self.assertFalse(out["half_available"])
        self.assertEqual(out["layers"], ["month", "xun", "week"])

    @mock.patch.object(api, "_xw_ready", return_value=False)
    @mock.patch.object(api, "_half_ready")
    def test_unavailable_keeps_existing_error_shape(self, half, _xw):
        out = api.window_rules_status(_NoQueryConnection(), lite=True)
        self.assertEqual(out, {"available": False, "note": "未找到月/旬/段规则表"})
        half.assert_not_called()


if __name__ == "__main__":
    unittest.main()
