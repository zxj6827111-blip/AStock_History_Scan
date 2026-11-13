"""流月卦起卦器：冻结副本校验 + 与参考系统的公式同一性测试。"""
from __future__ import annotations

import importlib.util
import random
import sys
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE / "app"))

import bagua as B  # noqa: E402  导入即触发 manifest sha256 校验

REF_CALC = Path("/Volumes/workSSD/software development/Analysis-of-A-shares/"
                "wtpy/apps/astock/bagua/calculator.py")


def _load_reference():
    spec = importlib.util.spec_from_file_location("ref_bagua_calculator", REF_CALC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses 需要模块已注册才能解析 KW_ONLY
    spec.loader.exec_module(mod)
    return mod.BaguaCalculator.from_json(REF_CALC.parent / "bagua_384.json")


def _equal(a: dict, b: dict) -> bool:
    return a == b


class TestFrozenCopy(unittest.TestCase):
    def test_golden_vector(self):
        r = B.cast_ohlc(6.27, 7.33, 5.9, 5.9)
        self.assertEqual(r.upper_id, 7)
        self.assertEqual(r.lower_id, 6)
        self.assertEqual(r.yao_order, 3)
        self.assertEqual(r.gua_name, "山水蒙")
        self.assertEqual(r.to_dict()["state_id"], "04-3")

    def test_stance_map(self):
        for sig, side in {"新开仓": "好", "加仓": "好", "持有或开仓": "中",
                          "持有": "中", "观察": "中", "减仓": "差",
                          "不碰": "差", "清仓": "差"}.items():
            self.assertEqual(B.gua_side(sig), side, sig)


class TestFormulaIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = _load_reference()

    def _compare(self, o, h, l, c):
        mine = B.cast_ohlc(o, h, l, c).to_dict()
        theirs = self.ref.calculate(open_price=o, high_price=h,
                                    low_price=l, close_price=c).to_dict()
        self.assertTrue(_equal(mine, theirs),
                        f"OHLC={(o, h, l, c)} 结果不一致")

    def test_edge_cases(self):
        cases = [
            (1.005, 1.005, 1.005, 1.005),   # ROUND_HALF_UP 舍入边界
            (8.00, 8.00, 8.00, 8.00),       # 数字和=8 → mod 余 0 取 8
            (17.00, 6.00, 6.00, 17.00),     # 开/收和=8，高+低=12 → 动爻余 0 取 6
            (0.01, 0.01, 0.01, 0.01),
            (9999.99, 9999.99, 9999.99, 9999.99),
            (6.27, 7.33, 5.9, 5.9),         # 冻结 golden
            (3000.0, 3050.5, 2990.25, 3010.1),
        ]
        for o, h, l, c in cases:
            self._compare(o, h, l, c)

    def test_random_identity(self):
        rng = random.Random(20261111)
        for _ in range(500):
            o = round(rng.uniform(0.5, 3000), rng.choice([0, 1, 2, 3]))
            c = round(rng.uniform(0.5, 3000), rng.choice([0, 1, 2, 3]))
            h = round(max(o, c) * rng.uniform(1.0, 1.1), 2)
            l = round(min(o, c) * rng.uniform(0.9, 1.0), 2)
            self._compare(o, h, l, c)

    def test_harness_detects_difference(self):
        # 正对照：比较器必须能翻脸，否则同一性测试是空转
        r = B.cast_ohlc(6.27, 7.33, 5.9, 5.9).to_dict()
        bad = dict(r)
        bad["upper_id"] = (r["upper_id"] % 8) + 1
        self.assertFalse(_equal(r, bad))


class TestGaodaoConsensus(unittest.TestCase):
    """高岛易断（第二套解读）+ 双好/双差共识：与参考系统同源同词表。"""

    def test_golden_gaodao(self):
        r = B.cast_ohlc(6.27, 7.33, 5.9, 5.9)  # 04-3 山水蒙 六三
        self.assertEqual(r.state_id, "04-3")
        gt = B.gaodao_text(r.state_id)
        self.assertTrue(gt, "04-3 应有营商断语")
        self.assertIn("行商", gt)
        self.assertEqual(B.gaodao_category(r.state_id), "营商")
        self.assertFalse(B.gaodao_is_fallback(r.state_id))
        self.assertEqual(B.gua_side(r.action_signal), "中")     # 持有
        self.assertEqual(B.gaodao_side(gt), "不明")             # 文言转折 → 不明
        self.assertEqual(B.consensus(r.action_signal, gt), "")  # 一方中性 → 无共识

    def test_consensus_mapping(self):
        self.assertEqual(B.consensus("新开仓", "大利获利，必得利"), "▲双好")
        self.assertEqual(B.consensus("清仓", "凶，损失，败"), "▼双差")
        self.assertEqual(B.consensus("新开仓", "凶，损失"), "分歧")
        self.assertEqual(B.consensus("持有", "大利"), "")        # 卦象中性
        self.assertEqual(B.consensus("新开仓", ""), "")          # 高岛不明

    def test_known_states(self):
        self.assertEqual(B.consensus("清仓", B.gaodao_text("01-6")), "▼双差")   # 乾为天 上九
        self.assertEqual(B.consensus("新开仓", B.gaodao_text("02-5")), "▲双好")  # 坤为地 六五

    def test_coverage(self):
        cov = B.gaodao_coverage()
        self.assertEqual(cov["state_total"], 384)
        self.assertGreater(cov["total"], 300)  # 379 条断语


if __name__ == "__main__":
    unittest.main()