"""P1 验收：十神纯函数、12 支藏干、510050 黄金样本。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import ten_god_rules as R          # noqa: E402
import ten_god_service as SVC      # noqa: E402


class TenGodMappingTest(unittest.TestCase):
    def test_100_frozen_entries(self):
        """10 日干 × 10 天干 = 100 组，推导结果与冻结映射逐条一致。"""
        pairs = [(dm, other) for dm in R.STEMS for other in R.STEMS]
        self.assertEqual(len(pairs), 100)
        bad = [f"{dm}>{o}" for dm, o in pairs if R.ten_god(dm, o) != REF[(dm, o)]]
        self.assertEqual(bad, [])

    def test_matrix_is_complete_and_named(self):
        gods = {R.ten_god(dm, o) for dm in R.STEMS for o in R.STEMS}
        self.assertEqual(gods, set(R.TEN_GODS))
        self.assertEqual(len(R.TEN_GODS), 10)

    def test_direction_guard(self):
        self.assertEqual(R.ten_god("癸", "丙"), "正财")
        self.assertEqual(R.ten_god("丙", "癸"), "正官")

    def test_wu_day_master_examples(self):
        expected = {"戊": "比肩", "己": "劫财", "庚": "食神", "辛": "伤官",
                    "壬": "偏财", "癸": "正财", "甲": "七杀", "乙": "正官",
                    "丙": "偏印", "丁": "正印"}
        for other, god in expected.items():
            self.assertEqual(R.ten_god("戊", other), god, f"戊×{other}")


class HiddenStemsTest(unittest.TestCase):
    def test_12_branches_complete(self):
        self.assertEqual(tuple(R.HIDDEN_STEMS), R.BRANCHES)
        self.assertEqual(len(R.HIDDEN_STEMS), 12)

    def test_stems_valid_and_unique(self):
        for branch, stems in R.HIDDEN_STEMS.items():
            self.assertTrue(stems, branch)
            self.assertEqual(len(set(stems)), len(stems), branch)
            for stem in stems:
                self.assertIn(stem, R.STEMS, f"{branch} 藏 {stem}")

    def test_order_matches_frozen_json(self):
        raw = json.loads(R.HIDDEN_PATH.read_text(encoding="utf-8"))
        for item in raw["branches"]:
            self.assertEqual(tuple(item["stems"]), R.HIDDEN_STEMS[item["branch"]])

    def test_wan_wu_hai_golden(self):
        self.assertEqual(R.hidden_stems("亥"), ("壬", "甲"))
        self.assertEqual(R.ten_god("戊", "壬"), "偏财")
        self.assertEqual(R.ten_god("戊", "甲"), "七杀")

    def test_one_two_three_roles(self):
        one = SVC.get_hidden_stem_ten_gods("戊", "子")["items"]
        self.assertEqual([x["role"] for x in one], ["本气"])
        two = SVC.get_hidden_stem_ten_gods("戊", "亥")["items"]
        self.assertEqual([x["role"] for x in two], ["本气", "其他藏干"])
        three = SVC.get_hidden_stem_ten_gods("戊", "寅")["items"]
        self.assertEqual([x["role"] for x in three], ["本气", "其他藏干1", "其他藏干2"])

    def test_no_weight_field(self):
        obj = SVC.get_pillar_ten_gods("戊", "己亥")
        text = json.dumps(obj, ensure_ascii=False)
        for banned in ("weight", "good_or_bad", "stock_direction_prediction", "score"):
            self.assertNotIn(banned, text)


class Golden510050Test(unittest.TestCase):
    def test_profile_case(self):
        """本命乙酉/戊寅/戊寅，日主戊；己亥流月天干劫财，亥藏壬偏财、甲七杀。"""
        obj = SVC.get_pillar_ten_gods("戊", "己亥")
        self.assertEqual(obj["status"], "OK")
        self.assertEqual(obj["stem"]["ten_god"], "劫财")
        self.assertEqual([(h["stem"], h["ten_god"]) for h in obj["hidden_stems"]],
                         [("壬", "偏财"), ("甲", "七杀")])
        self.assertEqual(obj["hidden_summary"], "壬·偏财 / 甲·七杀")
        self.assertFalse(obj["prediction"])

    def test_chinese_names_survive_json(self):
        obj = SVC.get_pillar_ten_gods("戊", "己亥")
        text = json.dumps(obj, ensure_ascii=False)
        self.assertIn("劫财", text)
        self.assertIn("偏财", text)
        self.assertIn("七杀", text)


class ControlledErrorTest(unittest.TestCase):
    def test_invalid_day_master(self):
        with self.assertRaises(R.TenGodRuleError):
            R.ten_god("X", "甲")
        obj = SVC.get_pillar_ten_gods("X", "己亥")
        self.assertEqual(obj["status"], "UNAVAILABLE_PROFILE")
        self.assertIsNone(obj["stem"])
        self.assertEqual(obj["hidden_stems"], [])

    def test_missing_day_master(self):
        for dm in (None, "", "戊土"):
            self.assertEqual(SVC.get_pillar_ten_gods(dm, "己亥")["status"],
                             "UNAVAILABLE_PROFILE")

    def test_invalid_pillar(self):
        for pillar in ("己", "己亥子", "甲丑", "己X", None):
            obj = SVC.get_pillar_ten_gods("戊", pillar)
            self.assertEqual(obj["status"], "INVALID_PILLAR", repr(pillar))

    def test_no_unknown_god_output(self):
        """非法输入不得输出“未知吉凶”“神煞”等占位十神。"""
        obj = SVC.get_pillar_ten_gods(None, "己亥")
        text = json.dumps(obj, ensure_ascii=False)
        self.assertNotIn("未知", text)
        self.assertNotIn("吉", text)


def _load_reference():
    raw = json.loads(R.REFERENCE_PATH.read_text(encoding="utf-8"))
    out = {}
    for key, god in raw["entries"].items():
        dm, other = key.split(">")
        out[(dm, other)] = god
    return out


REF = _load_reference()

if __name__ == "__main__":
    unittest.main(verbosity=2)
