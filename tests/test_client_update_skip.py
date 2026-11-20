"""客户机更新器容错回归：白名单外/越界条目跳过、不中断整包（2026-11-20）。

背景：发布端新增页面数据（如 survivor_report_v1/reports/）时，旧客户机不能因此
整包拒收、连数据都停更——更新器逐条目跳过不认识/越界的条目即可，安全边界不变
（从不向允许目录外写文件）。新老客户机可并存，无更新时限。

跑法：python3 -m unittest -v tests.test_client_update_skip（在 tengod 根目录）
"""
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

CU = Path(__file__).resolve().parent.parent / "tools" / "client_update.py"


def _load():
    spec = importlib.util.spec_from_file_location("client_update_under_test", CU)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestApplyPackageSkip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        self.zip = Path(self._tmp.name) / "update_20261120.zip"
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("source_input/live/current_month.sqlite", b"db")
            zf.writestr("survivor_report_v1/reports/latest.json", b"{}")
            zf.writestr("future_feature_v1/data/x.json", b"{}")
            zf.writestr("../evil.txt", b"x")
            zf.writestr("/etc/evil.txt", b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_client_applies_known_and_skips_unknown(self):
        cu = _load()
        count, skipped = cu.apply_package(self.root, self.zip, "2026-11-20")
        self.assertEqual(count, 2)              # 当月层 + 生存者报告
        self.assertEqual(len(skipped), 3)       # 未知目录 + 两个越界
        self.assertTrue((self.root / "survivor_report_v1/reports/latest.json").is_file())
        self.assertFalse((self.root.parent / "evil.txt").exists())
        self.assertFalse((Path("/etc/evil.txt")).exists())

    def test_old_whitelist_still_updates_data(self):
        """旧客户机（白名单还没有 survivor）遇到带报告的新包：数据照常更新，不报错。"""
        cu = _load()
        cu.ALLOWED_PREFIXES = tuple(p for p in cu.ALLOWED_PREFIXES
                                    if "survivor" not in p)
        count, skipped = cu.apply_package(self.root, self.zip, "2026-11-20")
        self.assertEqual(count, 1)              # 数据文件照常落地
        self.assertFalse((self.root / "survivor_report_v1").exists())
        self.assertTrue(skipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
