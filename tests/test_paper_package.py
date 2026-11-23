"""模拟盘更新包集成测试：公开快照入包清单 + 客户机白名单落地（新老并存）。

跑法（tengod 根目录）：python3 -m unittest -v tests.test_paper_package
"""
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]           # bazi_month_relation_tengod_dev_v1/
OUTER = REPO.parent                                   # AStock_History_Scan/
BUP = OUTER / "scripts" / "build_update_package.py"
CU = REPO / "tools" / "client_update.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPaperPackage(unittest.TestCase):
    def test_paper_public_files_collect(self):
        bup = _load(BUP, "bup_under_test")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "bazi_month_relation_tengod_dev_v1" / "data" / "paper_public"
            (d / "plans").mkdir(parents=True)
            self.assertEqual(bup.paper_public_files(root, "2026-11-24"), [])
            (d / "latest.json").write_text("{}", encoding="utf-8")
            (d / "nav.jsonl").write_text("\n", encoding="utf-8")
            (d / "plans" / "latest.json").write_text("{}", encoding="utf-8")
            (d / "plans" / "2026-11-24.json").write_text("{}", encoding="utf-8")
            files = bup.paper_public_files(root, "2026-11-24")
            self.assertEqual(len(files), 4)
            self.assertIn("bazi_month_relation_tengod_dev_v1/data/paper_public/"
                          "plans/2026-11-24.json", files)
            # 哈希稳定可入 manifest
            shas = bup.report_shas(root, files)
            self.assertEqual(set(shas), set(files))
            self.assertTrue(all(len(v) == 64 for v in shas.values()))

    def test_collect_includes_paper_snapshot(self):
        bup = _load(BUP, "bup_under_test")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in bup.CORE_FILES:      # 核心文件占位（collect 不拦即可）
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            d = root / "bazi_month_relation_tengod_dev_v1" / "data" / "paper_public"
            (d / "plans").mkdir(parents=True)
            (d / "latest.json").write_text("{}", encoding="utf-8")
            files, missing_core, missing_opt = bup.collect(root, 0, False,
                                                           "2026-11-24")
            self.assertEqual(missing_core, [])
            self.assertIn("bazi_month_relation_tengod_dev_v1/data/paper_public/"
                          "latest.json", files)
            # plans/latest.json 缺失 → 只进 missing_optional，不拦包
            self.assertIn("bazi_month_relation_tengod_dev_v1/data/paper_public/"
                          "plans/latest.json", missing_opt)

    def test_client_applies_paper_snapshot(self):
        """客户机白名单已覆盖仓库前缀：模拟盘快照照常落地，未知目录跳过。"""
        cu = _load(CU, "client_update_under_test")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            zp = Path(td) / "update_20261124.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("source_input/live/current_month.sqlite", b"db")
                zf.writestr("bazi_month_relation_tengod_dev_v1/data/paper_public/"
                            "latest.json", "{}")
                zf.writestr("bazi_month_relation_tengod_dev_v1/data/paper_public/"
                            "plans/2026-11-24.json", "{}")
                zf.writestr("future_feature_v1/data/x.json", b"{}")
            count, skipped = cu.apply_package(root, zp, "2026-11-24")
            self.assertEqual(count, 3)
            self.assertTrue((root / "bazi_month_relation_tengod_dev_v1" / "data"
                            / "paper_public" / "latest.json").is_file())
            self.assertTrue((root / "bazi_month_relation_tengod_dev_v1" / "data"
                            / "paper_public" / "plans" / "2026-11-24.json").is_file())
            self.assertTrue(skipped)   # 未知目录只跳过不中断


if __name__ == "__main__":
    unittest.main(verbosity=2)
