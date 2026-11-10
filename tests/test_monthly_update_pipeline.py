#!/usr/bin/env python3
"""T2 月度更新编排器自测（全部在 mktemp 假树里；绝不碰真库 / 真 NAS / 真批次 / dist）。

覆盖：
  1. --dry-run 逐节点打印 + 断言真位置零写入（并断言没有任何子进程被执行）；
  2. G_A 三种输入（发布相同 / 已变但交易日不够 / 已变且交易日够）各自结论；
  3. G_B 篡改一个封印件哈希 → 必须 FAIL；写封印件的意图 → SEALED_TARGET_CONFLICT；
  4. G_D 伪造余量不足 → DISK_SPACE_LOW；副本回读不一致 / 副本目标已存在 → FAIL；
  5. G_E 规则版本改成别的串 → 必须拒跑；
  6. --accept-gates 双向断言（五闸全过才传 --accept；故意让一闸失败 ⇒ 没传）；
  7. 原子替换：未放行 / 失败时正式文件字节不变，放行后 os.replace 生效；
  8. 状态 json 原子写、所有 *_bjt 后缀时间都是 +08:00、无 .tmp 残留、接管调度器初态。

运行：python3 -m unittest -v tests.test_monthly_update_pipeline
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import io
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import run_monthly_update as R  # noqa: E402

MARKERS = "execution_markers.txt"
STAGE_FILES = ["stage1_schema_profile.py", "stage2_facts_relations.py", "stage3_stats.py",
               "stage4_validate.py", "stage5_display_order.py", "stage6_reports.py",
               "stage7_acceptance_md.py"]
ETF_FILES = ["01_fetch_source.py", "02_build_read_layer.py", "03c_full_manifest.py",
             "04f_build_full_facts.py", "05f_build_full_relations.py", "06f_build_full_db.py",
             "07f_validate_full.py", "09_build_etf_stats.py", "10_validate_etf_stats.py"]

CAL_ROWS = [
    ("2026-M09", "白露", "2026-09-07T23:31:04+08"),
    ("2026-M10", "寒露", "2026-10-08T09:12:04+08"),
    ("2026-M10b", "霜降", "2026-10-23T18:17:04+08"),
    ("2026-M11", "立冬", "2026-11-07T17:52:04+08"),
    ("2026-M12", "大雪", "2026-12-07T05:10:04+08"),
    ("2027-M01", "小寒", "2027-01-05T21:14:04+08"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def stage_db_sqlite(p: Path, marker: str, tag: str) -> None:
    """假 stage/ETF 脚本写的 staging：必须是**真 sqlite**（G-F 只认数据库，
    文本桩会被判 STAGING_NOT_VERIFIABLE ⇒ 拒绝替换，这正是门禁应有的行为）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(str(p))
    con.execute("DROP TABLE IF EXISTS builder_marker")
    con.execute("CREATE TABLE builder_marker(k TEXT PRIMARY KEY, v TEXT, tag TEXT)")
    con.execute("INSERT INTO builder_marker VALUES('last_writer',?,?)", (marker, tag))
    con.commit()
    con.close()


def db_marker(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = con.execute("SELECT v FROM builder_marker WHERE k='last_writer'").fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""
    finally:
        con.close()


def cal_sqlite(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE calendar_months(month_id TEXT, term TEXT, start_at TEXT)")
    con.executemany("INSERT INTO calendar_months VALUES(?,?,?)", CAL_ROWS)
    con.execute("CREATE TABLE monthly_facts(x)")
    con.execute("INSERT INTO monthly_facts VALUES(1)")
    con.commit()
    con.close()
    return p


class FakeTree(unittest.TestCase):
    """最小假树：假 README / 假 zip / 假批次 / 假 sqlite / 假脚本（都只写假树内）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mu_test_"))
        cls.proj = cls.tmp / "proj"
        cls.docs = cls.tmp / "docs"
        cls.dev = cls.proj / "bazi_month_relation_tengod_dev_v1"
        cls.shadow_old = cls.proj / "v13_shadow_fullmarket_20261018"
        cls.rc = cls.proj / "v13_shadow_ready_20261018/release_candidate"
        cls.rel = cls.docs / "bazi_history_relation_v1"
        cls.lv1 = cls.proj / "bazi_month_relation_local_v1"
        cls.etf = cls.proj / "bazi_month_relation_etf_v1"
        cls.daily = cls.docs / "source_mirror/正式源/全A日K_合并版"
        # 用一个裸实例建树，建树过程中的 self.touch/self.py_script 走正常绑定；
        # 建完把实例属性铺回类，供所有测试实例共用。
        builder = cls.__new__(cls)
        builder._build()
        for k, v in builder.__dict__.items():
            setattr(cls, k, v)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------- 建树工具 ----------------
    def touch(self, p: Path, text: str = "x") -> Path:
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def py_script(self, p: Path, body: str) -> Path:
        head = (
            "import os, sys, json, hashlib, pathlib, sqlite3, zipfile, io\n"
            f"ROOT = pathlib.Path({str(self.tmp)!r})\n"
            f"open(str(ROOT / {MARKERS!r}), 'a').write(os.path.basename(__file__) + os.linesep)\n"
            "def _mk(db, tag):\n"
            "    exists = db.exists()\n"
            "    con = sqlite3.connect(str(db))\n"
            "    if not exists:\n"
            "        # 没有 seed（例如 --mode full）：整库重建路径，自己造表\n"
            "        con.execute('CREATE TABLE IF NOT EXISTS calendar_months(month_id TEXT,'\n"
            "                    ' term TEXT, start_at TEXT)')\n"
            "        con.execute('DROP TABLE IF EXISTS monthly_facts')\n"
            "        con.execute('CREATE TABLE monthly_facts(x)')\n"
            "        con.execute('INSERT INTO monthly_facts VALUES(1)')\n"
            "    con.execute('CREATE TABLE IF NOT EXISTS builder_marker(k TEXT PRIMARY KEY,'\n"
            "                ' v TEXT, tag TEXT)')\n"
            "    con.execute('INSERT OR REPLACE INTO builder_marker VALUES(?,?,?)',\n"
            "                ('last_writer', os.path.basename(__file__), tag))\n"
            "    con.commit(); con.close()\n"
        )
        return self.touch(p, head + body + "\n")

    def bash_script(self, p: Path, body: str) -> Path:
        return self.touch(
            p,
            "#!/bin/bash\nset -u\n"
            f'echo "$(basename "$0") $*" >> "{self.tmp}/{MARKERS}"\n' + body + "\n")

    def _build(self):
        # ---- 镜像（正式源）+ 冻结批 + 派生源
        zip_path = self.daily / "2026.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("2026/000001.SZ.csv",
                        "code,datetime,open,close\n"
                        "000001.SZ,2026-10-08,10,11\n"
                        "000001.SZ,2026-10-09,11,12\n"
                        "000001.SZ,2026-11-09,12,13\n")
        self.touch(self.daily / "README.json", json.dumps({"规模": {"跨度": "19901219..20261014"}}))
        man = {"source_version": "FAKE_NAS_20261015", "fetched_at_utc": "2026-10-15T00:00:00Z",
               "files": [{"path": "正式源/全A日K_合并版/2026.zip",
                          "sha256": sha(zip_path), "bytes": zip_path.stat().st_size}]}
        self.touch(self.docs / "source_mirror/source_manifest.json", json.dumps(man))
        self.batch = self.docs / "batches/20261016T174105-ff8959dd37"
        cal_sqlite(self.batch / "data.sqlite")
        self.touch(self.batch / "manifest.json", "{}")
        self.touch(self.batch / "reports/golden_report.json", '{"status":"PASS"}')
        self.touch(self.docs / "source_input/nas_source.duckdb", "FAKE-DUCKDB")
        self.touch(self.docs / "source_input/stock_master.csv", "stock_id\n")

        # ---- docs/scripts 四个既有脚本的假件（只写假树）
        self.py_script(self.docs / "scripts/fetch_nas_source.py", """
DAILY = pathlib.Path(@DAILY@)
MAN = pathlib.Path(@MAN@)
zp = DAILY / '2026.zip'
body = 'NEW-PUBLICATION-ROWS\\ncode,datetime,open,close\\n000001.SZ,2026-11-09,10,11\\n'
import io
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as zf:          # 固定条目时间戳 ⇒ 发布态可复现（否则每次哈希都变）
    zi = zipfile.ZipInfo('2026/000001.SZ.csv', date_time=(2026, 11, 9, 0, 0, 0))
    zf.writestr(zi, body)
zp.write_bytes(buf.getvalue())
doc = {'source_version': 'FAKE_NAS_REFRESH', 'fetched_at_utc': '2026-11-10T00:00:00Z',
       'files': [{'path': '正式源/全A日K_合并版/2026.zip',
                  'sha256': hashlib.sha256(zp.read_bytes()).hexdigest(),
                  'bytes': zp.stat().st_size}]}
MAN.write_text(json.dumps(doc), encoding='utf-8')
(DAILY / 'README.json').write_text(json.dumps({'规模': {'跨度': '19901219..20261109'}}),
                                   encoding='utf-8')
""".replace("@DAILY@", repr(str(self.daily))).replace("@MAN@", repr(str(self.docs / "source_mirror/source_manifest.json"))))
        self.py_script(self.docs / "scripts/build_source_input.py", """
p = pathlib.Path(@DB@)
p.write_text('REBUILT-DUCKDB', encoding='utf-8')
(p.parent / 'build_stats.json').write_text('{}', encoding='utf-8')
""".replace("@DB@", repr(str(self.docs / "source_input/nas_source.duckdb"))))
        self.py_script(self.docs / "scripts/build_master.py", """
out = pathlib.Path(@CSV@)
out.write_text('stock_id\\n000001.SZ\\n', encoding='utf-8')
open(str(ROOT / 'as_of_seen.txt'), 'w').write(os.environ.get('AS_OF', 'NONE'))
""".replace("@CSV@", repr(str(self.docs / "source_input/stock_master.csv"))))
        self.py_script(self.docs / "scripts/independent_verify.py", """
batch = pathlib.Path(sys.argv[1])
r = batch / 'reports'
r.mkdir(parents=True, exist_ok=True)
(r / 'independent_recomputation.json').write_text(json.dumps(
    {'status': 'PASS', 'difference_count': 0, 'independent_recomputations': 1234}),
    encoding='utf-8')
""")

        # ---- 主库 stage1..7 假件（尊重 LOCAL_DB_OUT）
        cal_sqlite(self.lv1 / "database/bazi_month_relation_local_v1.sqlite")
        cal_sqlite(self.etf / "database/bazi_month_relation_etf_v1.sqlite")
        stage_body = """
out = os.environ.get('LOCAL_DB_OUT')
if out:
    p = pathlib.Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    _mk(p, 'STAGED-MAIN-')
"""
        for s in STAGE_FILES:
            self.py_script(self.lv1 / "scripts" / s, stage_body)
        self.touch(self.lv1 / "work/validation.json", "{}")

        # ---- 影子跑批：封印件（20261018）+ 原始启动器
        sh_db = self.touch(self.shadow_old / "v13_full_shadow.sqlite", "SEALED-SHADOW-DB")
        sh_60 = self.touch(self.shadow_old / "v13_full_60pillar_history.csv", "SEALED-60")
        sh_rel = self.touch(self.shadow_old / "v13_full_relation_type_history.csv", "SEALED-REL")
        rev3 = self.touch(self.proj / "v13_shadow_ready_20261018/work/input/v13_staging_input_all_rev3.csv",
                          "SEALED-REV3-CSV")
        gates = dict.fromkeys(R.SHADOW_GATE_PASS_KEYS, "PASS")
        gates.update(dict.fromkeys(R.SHADOW_GATE_YES_KEYS, "YES"))
        gates["PRODUCTION_READY"] = "NO"
        sh_man = self.touch(self.shadow_old / "v13_full_shadow_manifest.json", json.dumps({
            "gates": gates, "verification": {"verdict": "PASS"},
            "files": [{"path": sh_db.name, "sha256": sha(sh_db)},
                      {"path": sh_60.name, "sha256": sha(sh_60)},
                      {"path": sh_rel.name, "sha256": sha(sh_rel)}],
            "input": {"path": str(rev3), "sha256": sha(rev3)},
        }))
        self.touch(self.shadow_old / "v13_full_shadow_manifest.sha256",
                   f"{sha(sh_man)}  v13_full_shadow_manifest.json\n")
        self.bash_script(self.shadow_old / "scripts/run_full_shadow.sh", f"""
TASK="{self.shadow_old}"
INPUT="{rev3}"
mkdir -p "$TASK"
[ -f "$TASK/v13_full_shadow.sqlite" ] || echo 'RUN-SHADOW-DB' > "$TASK/v13_full_shadow.sqlite"
printf '%s' '{{"gates": {{"PRE_RUN_RECONCILIATION": "PASS", "FULL_SHADOW_STAGE_A": "PASS", "FULL_SHADOW_STAGE_B": "PASS", "FULL_SHADOW_KEYSET": "PASS", "FULL_SHADOW_AB_LEAKAGE": "PASS", "FULL_SHADOW_VERSION_ISOLATION": "PASS", "FULL_SHADOW_FROZEN_UNCHANGED": "YES", "RESEARCH_SHADOW_COMPLETE": "YES", "PRODUCTION_READY": "NO", "relation_rule_version": "bazi-relation-research-v1.3.0-provisional"}}, "verification": {{"verdict": "PASS"}}, "input": {{"path": "'"$INPUT"'"}}}}' > "$TASK/v13_full_shadow_manifest.json"
echo "shadow run wrote $TASK/v13_full_shadow_manifest.json"
""")

        # ---- frozen_baseline.txt（7 件绝对路径）
        dist_db = self.touch(self.rel / "dist/history_stock_relations_v1.sqlite", "SEALED-REL-DB")
        dist_rep = self.touch(self.rel / "dist/relation_quality_report.json", "{}")
        lines = [f"{sha(sh_db)}  {sh_db}", f"{sha(sh_60)}  {sh_60}", f"{sha(sh_rel)}  {sh_rel}",
                 f"{sha(sh_man)}  {sh_man}", f"{sha(self.batch / 'data.sqlite')}  {self.batch / 'data.sqlite'}",
                 f"{sha(self.batch / 'manifest.json')}  {self.batch / 'manifest.json'}",
                 f"{sha(dist_db)}  {dist_db}"]
        self.touch(self.lv1 / "work/frozen_baseline.txt", "\n".join(lines) + "\n")

        # ---- release_candidate（含封印 SHA256SUMS + 规则版本）
        eng = self.touch(self.rc / "engine.py", "RC-ENGINE")
        pol = self.touch(self.rc / "rules/policy_research_default.json",
                         json.dumps({"relation_rule_version": R.RULE_VERSION_DEFAULT}))
        self.touch(self.rc / "SHA256SUMS.txt",
                   f"{sha(eng)}  engine.py\n{sha(pol)}  rules/policy_research_default.json\n")
        self.touch(self.rel / "dist/SHA256SUMS.txt",
                   f"{sha(dist_db)}  history_stock_relations_v1.sqlite\n"
                   f"{sha(dist_rep)}  relation_quality_report.json\n")
        self.py_script(self.rc / "tools/from_readonly_sqlite.py", """
out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('staging_id\\n000001.SZ,2026-M11\\n', encoding='utf-8')
""")

        # ---- 关系库里程碑脚本（假）
        (self.rel / "snapshot").mkdir(parents=True, exist_ok=True)
        (self.rel / "out").mkdir(parents=True, exist_ok=True)
        (self.rel / "evidence").mkdir(parents=True, exist_ok=True)
        self.touch(self.rel / "snapshot/period.jsonl", "{}\n")
        rel_body = """
RELROOT = pathlib.Path(@REL@)
name = os.path.basename(__file__)
out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]) if '--out' in sys.argv else RELROOT / 'out/run_a.sqlite3'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('REL-BUILD-' + name, encoding='utf-8')
if 'm6' in name:
    fin = RELROOT / 'dist/history_stock_relations_v1.sqlite'
    fin.write_text('REPUBLISHED-' + name, encoding='utf-8')
    s = fin.parent / 'SHA256SUMS.txt'
    keep = [l for l in s.read_text().splitlines() if not l.endswith('history_stock_relations_v1.sqlite')]
    keep.append('%s  history_stock_relations_v1.sqlite' % hashlib.sha256(fin.read_bytes()).hexdigest())
    s.write_text(os.linesep.join(keep) + os.linesep, encoding='utf-8')
    # 人工裁定的连带动作：重发 local_v1/work/frozen_baseline.txt 里那一行
    fb = pathlib.Path(@FB@)
    rows = [l for l in fb.read_text().splitlines() if l.strip()]
    rows = [l for l in rows if not l.endswith(str(fin))]
    rows.append('%s  %s' % (hashlib.sha256(fin.read_bytes()).hexdigest(), fin))
    fb.write_text(os.linesep.join(rows) + os.linesep, encoding='utf-8')
""".replace("@REL@", repr(str(self.rel))).replace("@FB@", repr(str(self.lv1 / 'work/frozen_baseline.txt')))
        for t in ("m4a_prepare_snapshot.py", "m4b_generate.py", "m5_verify_gates.py",
                  "m6_publish.py"):
            self.py_script(self.rel / "tools" / t, rel_body)

        # ---- ETF 支路假件（尊重 ETF_DB_OUT）
        self.bash_script(self.etf / "scripts/00_bootstrap.sh", "echo bootstrap-ok")
        etf_body = """
out = os.environ.get('ETF_DB_OUT')
if out:
    p = pathlib.Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    _mk(p, 'STAGED-ETF-')
"""
        for s in ETF_FILES:
            self.py_script(self.etf / "scripts" / s, etf_body)

        # ---- 扩展联动：把收到的 argv 记录下来（--accept 双向断言的关键）
        self.bash_script(self.lv1 / "extensions/update_all_after_main_db.sh",
                         f'echo "$@" > "{self.tmp}/extensions_argv.txt"\nexit 0\n')

        # ---- v5 切片假件
        self.py_script(self.proj / "v5_lowpoint_ui_isolated/tools/export_v5_slice.py", """
out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('SLICE', encoding='utf-8')
""")

        # ---- 假扫描器：只新建批次目录
        self.scanner = self.tmp / "fake_scanner.py"
        self.py_script(self.scanner, """
DOCS = pathlib.Path(os.environ['FAKE_DOCS'])
seq = int(os.environ.get('FAKE_SEQ', '0'))
new = DOCS / 'batches' / ('MUFAKE-%d' % seq)
(new / 'reports').mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(new / 'data.sqlite'))
con.execute('CREATE TABLE calendar_months(month_id TEXT, term TEXT, start_at TEXT)')
con.commit(); con.close()
(new / 'reports/golden_report.json').write_text('{"status":"PASS"}', encoding='utf-8')
""")

        # ---- dev 骨架
        for d in ("work", "logs", "reports", "backups", "data"):
            (self.dev / d).mkdir(parents=True, exist_ok=True)

    # ---------------- 运行 ----------------
    def paths(self) -> R.Paths:
        return R.Paths(project_root=self.proj, docs_root=self.docs, dev_root=self.dev)

    def env(self, extra=None):
        e = dict(os.environ)
        # 关键：三根目录一律指向假树 + 显式授权改根真跑，绝不允许打到真实链路
        e["MONTHLY_UPDATE_PROJECT_ROOT"] = str(self.proj)
        e["MONTHLY_UPDATE_DOCS_ROOT"] = str(self.docs)
        e["MONTHLY_UPDATE_DEV_ROOT"] = str(self.dev)
        e["MONTHLY_UPDATE_ALLOW_ALT_ROOT"] = "1"
        e["SCANNER_CLI"] = str(self.scanner)
        e["FAKE_DOCS"] = str(self.docs)
        e[R.MIN_FREE_ENV] = "1024"            # 假树里绕过 40GiB 余量下限
        if extra:
            e.update(extra)
        return e

    def cli(self, argv, extra_env=None):
        buf = StringIO()
        saved = dict(os.environ)
        seq = str(int(time.time() * 1_000_000) % 10_000_000)
        os.environ.update(self.env(dict(extra_env or {}, FAKE_SEQ=seq)))
        try:
            with redirect_stdout(buf):
                rc = R.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return rc, buf.getvalue()

    def must_cli(self, argv, extra_env=None, expect_rc=None):
        rc, out = self.cli(argv, extra_env)
        if expect_rc is not None:
            self.assertEqual(rc, expect_rc, f"rc={rc} argv={argv}\n{out[-6000:]}")
        return rc, out

    def markers(self) -> str:
        f = self.tmp / MARKERS
        return f.read_text() if f.exists() else ""

    def argv_of_extensions(self) -> str:
        f = self.tmp / "extensions_argv.txt"
        return f.read_text().strip() if f.exists() else ""

    def cli2(self, argv, extra_env=None):
        """同上，但把 stderr 一起收进来（CLI 的拒启/对账结论走 stderr）。"""
        err = StringIO()
        old_err = sys.stderr
        sys.stderr = err
        try:
            rc, out = self.cli(argv, extra_env)
        finally:
            sys.stderr = old_err
        return rc, out + err.getvalue()

    def fresh_state(self):
        """每个 execute 用例都重置：假库回到有 calendar_months 的状态、清掉上一轮产物。"""
        P = self.paths()
        for f in (P.state_file, P.fingerprints, P.update_log_md, P.lock_file,
                  P.work / "last_batch.json", P.baseline_file, P.rule_lock,
                  P.drift_baseline):
            if f.exists():
                f.unlink()
        shutil.rmtree(P.backups, ignore_errors=True)
        shutil.rmtree(P.work / "runs", ignore_errors=True)
        shutil.rmtree(P.logs, ignore_errors=True)
        P.logs.mkdir(parents=True, exist_ok=True)
        P.backups.mkdir(parents=True, exist_ok=True)
        (self.tmp / MARKERS).unlink(missing_ok=True)
        (self.tmp / "extensions_argv.txt").unlink(missing_ok=True)
        for db in P.local_db_dir.glob("staging_*.sqlite"):
            db.unlink()
        for db in (P.etf_v1 / "database").glob("staging_*.sqlite"):
            db.unlink()
        for d in self.proj.glob("v13_shadow_fullmarket_20*"):
            if d.name != "v13_shadow_fullmarket_20261018":
                shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self.rel / "out", ignore_errors=True)
        (self.rel / "out").mkdir(parents=True, exist_ok=True)
        for d in self.docs.glob("batches/MUFAKE-*"):
            shutil.rmtree(d, ignore_errors=True)
        # 假库重置（保证日历可读、字节基线一致）
        cal_sqlite(P.main_db)
        cal_sqlite(P.etf_db)
        self.refreeze()
        return P

    def refreeze(self):
        """把 dist 库与其两处哈希登记恢复到「本轮开跑前」的样子（用例之间互不污染）。"""
        dist_db = self.rel / "dist/history_stock_relations_v1.sqlite"
        dist_db.write_text("SEALED-REL-DB", encoding="utf-8")
        rep = self.rel / "dist/relation_quality_report.json"
        rep.write_text("{}", encoding="utf-8")
        (self.rel / "dist/SHA256SUMS.txt").write_text(
            f"{sha(dist_db)}  history_stock_relations_v1.sqlite\n"
            f"{sha(rep)}  relation_quality_report.json\n", encoding="utf-8")
        sh_db = self.shadow_old / "v13_full_shadow.sqlite"
        sh_60 = self.shadow_old / "v13_full_60pillar_history.csv"
        sh_rel = self.shadow_old / "v13_full_relation_type_history.csv"
        sh_man = self.shadow_old / "v13_full_shadow_manifest.json"
        batch = self.batch
        lines = [f"{sha(p)}  {p}" for p in (sh_db, sh_60, sh_rel, sh_man,
                                           batch / "data.sqlite", batch / "manifest.json",
                                           dist_db)]
        (self.lv1 / "work/frozen_baseline.txt").write_text("\n".join(lines) + "\n",
                                                           encoding="utf-8")

    def write_baseline(self, P, sha_value="0" * 64):
        P.work.mkdir(parents=True, exist_ok=True)
        P.baseline_file.write_text(json.dumps({
            "schema": R.BASELINE_SCHEMA, "recorded_at_bjt": "2026-10-15T08:00:00+08:00",
            "max_trade_date": "2026-10-14",
            "publication_files": {"正式源/全A日K_合并版/2026.zip": {"size": 1, "sha256": sha_value}},
        }), encoding="utf-8")


class TestRootGuard(FakeTree):
    """防误触闸：假根真跑要显式开关，真根真跑要人工哨兵文件。"""

    def test_alt_root_execute_requires_optin(self):
        saved = dict(os.environ)
        e = self.env()
        e.pop("MONTHLY_UPDATE_ALLOW_ALT_ROOT")
        os.environ.clear()
        os.environ.update(e)
        err = StringIO()
        old_err = sys.stderr
        sys.stderr = err
        try:
            with redirect_stdout(buf := StringIO()):
                rc = R.main(["--run-id", "MU-20261109T210000", "--execute"])
        finally:
            sys.stderr = old_err
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(rc, 3)
        self.assertIn("ALT_ROOT_NOT_AUTHORIZED", err.getvalue())

    # 原 test_default_root_execute_refuses_without_sentinel 依赖「缺省回落到真树」，
    # 该缺省已被删除；等价（且更严）的断言搬到 TestNoRootConfigured：
    #   · test_no_root_refuses_startup_and_touches_nothing
    #   · test_real_tree_execute_still_needs_sentinel


class TestDryRun(FakeTree):
    """dry-run 的写入白名单（2026-10-22 fixround3 改了口径，故这两条断言同步改）：

    立项前：dry-run **一个字节都不写**，于是终态永远缺失 ⇒ 页面停在 RUNNING、
    `update_plan.json.last_status=RUNNING`、前端 5 秒一次无限轮询（用户可见死锁，P1-2）。
    现在：dry-run 只允许写编排器自有的两件状态件
      · `work/update_state.json`（必须带 finished_at_bjt + 终态 status + error{code,message}）
      · `logs/update_<run_id>.log`
    除这两件之外任何文件被改动，本条断言就失败（原来的「全树零改动」换成「全树只改这两件」，
    判据没有放松：范围是白名单，不是「随便写」）。
    """

    DRY_WHITELIST_LOG = "update_MU-20261022T090000.log"

    def _assert_only_own_state_changed(self, before, P, run_id):
        after = tree_snapshot(self.tmp)
        allowed = {str(P.state_file.relative_to(self.tmp)),
                   str((P.logs / f"update_{run_id}.log").relative_to(self.tmp))}
        changed = {k for k in set(after) | set(before)
                   if after.get(k) != before.get(k)}
        self.assertEqual(changed, allowed,
                         f"dry-run 写出了白名单外的文件：{sorted(changed - allowed)}")
        return after

    def test_dry_run_writes_only_own_state_and_executes_nothing(self):
        P = self.paths()
        run_id = "MU-20261022T090000"
        P.baseline_file.write_text(json.dumps({"publication_files": {}}), encoding="utf-8")
        self.addCleanup(P.baseline_file.unlink)
        before = tree_snapshot(self.tmp)
        rc, out = self.cli(["--run-id", run_id, "--trigger", "manual", "--dry-run"])
        after = self._assert_only_own_state_changed(before, P, run_id)
        self.assertTrue(P.state_file.exists(), "dry-run 必须落一份自有终态（P1-2）")
        st = json.loads(after and P.state_file.read_text(encoding="utf-8"))
        self.assertEqual(st.get("status"), "DRY_RUN_PLAN", "dry-run 终态应为 DRY_RUN_PLAN")
        self.assertTrue(str(st.get("finished_at_bjt") or "").endswith("+08:00"),
                        f"finished_at_bjt 必须是带 +08:00 的北京串：{st.get('finished_at_bjt')}")
        self.assertEqual(st.get("exit_rc"), 0, "dry-run 这一轮的退出码要照实登记")
        self.assertIn("write_conflicts", st, "写入目标冲突数必须带出来（预览要用它说话）")
        self.assertFalse(P.fingerprints.exists())
        self.assertFalse(P.update_log_md.exists())
        self.assertFalse(P.lock_file.exists())
        self.assertEqual(self.markers(), "", f"dry-run 竟执行了子进程：{self.markers()}")
        for i in range(1, 14):
            self.assertIn(f"NODE {i:>2} ", out, f"缺少节点 {i} 的打印")
        # 落盘登记逐条核对：白名单外不许有任何 WROTE/APPENDED（原来是靠 state 那条
        # WOULD WRITE 顺带证明的；现在 state 是真写的，就改成直接核对所有落盘登记行）。
        wrote = [l for l in out.splitlines() if "] WROTE " in l or "] APPENDED " in l]
        self.assertTrue(any("update_state.json" in l for l in wrote),
                        f"dry-run 应把自有状态件落盘：{wrote}")
        for l in wrote:
            self.assertTrue(l.rstrip().endswith(("update_state.json",
                                                 f"update_{run_id}.log")),
                            f"dry-run 写出了白名单外的文件：{l}")
        self.assertIn("未执行任何子进程", out)
        self.assertEqual(rc, 0)

    def test_dry_run_terminal_carries_gate_reason(self):
        """双向自证的负例半边：闸门没全过的 dry-run 轮，终态里必须看得见原因。"""
        P = self.paths()
        run_id = "MU-20261022T090050"
        # baseline 缺失 ⇒ 编排器 G_A 判 FAIL SOURCE_BASELINE_MISSING（现网就是这个状态）
        P.baseline_file.unlink(missing_ok=True)
        self.cli(["--run-id", run_id, "--trigger", "manual", "--dry-run"])
        st = json.loads(P.state_file.read_text(encoding="utf-8"))
        self.assertEqual(st.get("status"), "DRY_RUN_PLAN")
        self.assertTrue(st.get("finished_at_bjt"), "失败轮同样要有结束时刻")
        self.assertTrue((st.get("error") or {}).get("code"),
                        f"闸门未全过的 dry-run 必须把原因写进 error：{st.get('error')}")
        self.assertIn("G_A", str((st.get("error") or {}).get("message")))
        self.assertEqual((st.get("gates") or {}).get("G_A", {}).get("code"),
                         R.E_SOURCE_BASELINE_MISSING)

    def test_default_mode_is_dry_run(self):
        P = self.paths()
        run_id = "MU-20261022T090100"
        before = tree_snapshot(self.tmp)
        _, out = self.cli(["--run-id", run_id])
        self._assert_only_own_state_changed(before, P, run_id)
        self.assertIn("mode=dry-run", out)

    def test_only_node_is_echoed(self):
        _, out = self.cli(["--run-id", "MU-20261022T090200", "--only-node", "4,8"])
        self.assertIn("only=[4, 8]", out)

    def test_bad_run_id_with_colon_rejected(self):
        rc, out = self.cli(["--run-id", "MU-20261022T09:00:00"])
        self.assertEqual(rc, 2)

    def test_node5_commands_are_printed_but_sealed_targets_flagged(self):
        _, out = self.cli(["--run-id", "MU-20261022T090300"])
        self.assertIn("m6_publish.py", out)
        self.assertIn("SEALED_TARGET_CONFLICT", out)


class TestGateAPure(FakeTree):
    def _stats(self, digest, max_day="2026-10-14"):
        return {"available": True, "max_trade_date": max_day,
                "publication_files": {"正式源/全A日K_合并版/2026.zip": {"size": 10, "sha256": digest}}}

    def _base(self, digest):
        return {"publication_files": {"正式源/全A日K_合并版/2026.zip": {"size": 10,
                                                                       "sha256": digest}}}

    def test_same_publication_is_source_not_ready(self):
        g = R.gate_a(self._stats("a" * 64), self._base("a" * 64),
                     {"resolved": True, "month_id": "2026-M11", "first_trading_day": "2026-11-09"})
        self.assertEqual((g.verdict, g.code), ("FAIL", "SOURCE_NOT_READY"))
        self.assertFalse(g.detail["publication_changed"])

    def test_changed_but_trading_day_not_covered(self):
        g = R.gate_a(self._stats("b" * 64), self._base("a" * 64),
                     {"resolved": True, "month_id": "2026-M11", "first_trading_day": "2026-11-09"})
        self.assertEqual((g.verdict, g.code), ("FAIL", "SOURCE_NOT_READY"))
        self.assertTrue(g.detail["publication_changed"])
        self.assertIn("未覆盖目标流月", g.message)

    def test_changed_and_covered(self):
        g = R.gate_a(self._stats("b" * 64, "2026-11-09"), self._base("a" * 64),
                     {"resolved": True, "month_id": "2026-M11", "first_trading_day": "2026-11-09"})
        self.assertEqual(g.verdict, "PASS")

    def test_missing_baseline_blocks_unless_forced(self):
        target = {"resolved": True, "month_id": "2026-M11", "first_trading_day": "2026-11-09"}
        g = R.gate_a(self._stats("b" * 64), None, target)
        self.assertEqual((g.verdict, g.code), ("FAIL", "SOURCE_BASELINE_MISSING"))
        calls = []
        g2 = R.gate_a(self._stats("b" * 64), None, target, force_source_check=True,
                      write_baseline=lambda s: calls.append(1))
        self.assertEqual(g2.verdict, "PASS")
        self.assertEqual(calls, [1])

    def test_first_trading_day_uses_0930_anchor(self):
        P = self.paths()
        terms = R.resolve_calendar(P)
        self.assertTrue(terms)
        t = R.resolve_target_month(P, "2026-11-09", R.now_bjt(), terms)
        self.assertEqual(t["month_id"], "2026-M11")
        self.assertEqual(t["first_possible_bar_date"], "2026-11-08")   # 17:52 > 09:30 ⇒ 11-07 归上月
        self.assertEqual(t["first_trading_day"], "2026-11-09")         # 镜像里有 11-09 的行情日
        self.assertEqual(t["first_trading_day_basis"], "source_datetime")
        t2 = R.resolve_target_month(P, "2026-10-22", R.now_bjt(), terms)
        self.assertEqual(t2["month_id"], "2026-M10")


class TestGateB(FakeTree):
    def _ctx(self, run_id="MU-20261022T100000"):
        return R.Context(run_id=run_id, runday=run_id[3:11], trigger="manual", as_of="2026-10-14",
                         as_of_basis="detected", dry_run=True, execute=False, accept_gates=False,
                         only=[], force_source_check=False, relation_build_db=None,
                         target={"resolved": True, "month_id": "2026-M11",
                                 "first_trading_day": "2026-11-09"})

    def test_seals_valid_and_only_node5_blocks(self):
        P = self.paths()
        ctx = self._ctx()
        nodes = R.plan_nodes(P, ctx)
        g = R.gate_b(P, nodes, ctx.run_id, ctx.runday)
        self.assertEqual((g.verdict, g.code), ("FAIL", "SEALED_TARGET_CONFLICT"))
        self.assertTrue(any(c["node"] == 5 and "dist" in c["path"] for c in g.detail["conflicts"]))
        # 除节点 5 以外没有别的冲突：说明封印件本身全部哈希一致
        self.assertTrue(all(c["node"] == 5 for c in g.detail["conflicts"]),
                        json.dumps(g.detail, ensure_ascii=False)[:2000])
        self.assertGreater(len(R.collect_seals(self.paths())), 5)

    def test_tampered_seal_must_fail(self):
        P = self.paths()
        victim = self.shadow_old / "v13_full_60pillar_history.csv"
        old = victim.read_bytes()
        victim.write_bytes(b"TAMPERED")
        try:
            ctx = self._ctx("MU-20261022T100100")
            g = R.gate_b(P, R.plan_nodes(P, ctx), ctx.run_id, ctx.runday)
            self.assertEqual((g.verdict, g.code), ("FAIL", "SEAL_HASH_MISMATCH"))
            self.assertIn(str(victim), [m["path"] for m in g.detail["mismatches"]])
        finally:
            victim.write_bytes(old)

    def test_seal_list_missing_is_reported(self):
        P = self.paths()
        sums = P.relation_dist / "SHA256SUMS.txt"
        old = sums.read_bytes()
        sums.unlink()
        try:
            ctx = self._ctx("MU-20261022T100200")
            g = R.gate_b(P, R.plan_nodes(P, ctx), ctx.run_id, ctx.runday)
            self.assertEqual(g.verdict, "FAIL")
            self.assertIn(str(sums), [b["path"] for b in g.detail["mismatches"]])
        finally:
            sums.write_bytes(old)

    def test_run_scoped_and_new_batch_targets_allowed(self):
        P = self.paths()
        ctx = self._ctx("MU-20261022T100300")
        nodes = R.plan_nodes(P, ctx)
        nodes = [n for n in nodes if n.index != 5]          # 只看其它节点的写入意图
        g = R.gate_b(P, nodes, ctx.run_id, ctx.runday)
        self.assertEqual(g.verdict, "PASS", json.dumps(g.detail, ensure_ascii=False)[:2000])

    def test_existing_new_only_target_is_blocked(self):
        P = self.paths()
        ctx = self._ctx("MU-20261022T100400")
        exists = self.touch(self.dev / "data/exists.sqlite", "PRE-EXISTING")
        node = R.Node(99, "k", "t", writes=[R.WriteTarget(exists, new_only=True)])
        planned, conflicts = R.audit_write_targets(
            [node], sealed=set(), protected=R.protected_dirs(P), allow_inplace=set(),
            run_id=ctx.run_id, runday=ctx.runday, P=P)
        self.assertEqual(conflicts[0]["reason"], "new_only_target_exists")


class TestGateCDE(FakeTree):
    def _manifest(self, **override):
        gates = dict.fromkeys(R.SHADOW_GATE_PASS_KEYS, "PASS")
        gates.update(dict.fromkeys(R.SHADOW_GATE_YES_KEYS, "YES"))
        gates["PRODUCTION_READY"] = "NO"
        gates.update(override)
        return {"gates": gates, "verification": {"verdict": "PASS"}}

    def test_all_nine_gates_plus_verify_pass(self):
        g = R.gate_c(self._manifest(), {"status": "PASS", "difference_count": 0,
                                        "independent_recomputations": 10},
                     node7_ran=True, node11_ran=True)
        self.assertEqual(g.verdict, "PASS")

    def test_single_failed_shadow_gate(self):
        g = R.gate_c(self._manifest(FULL_SHADOW_KEYSET="FAIL"),
                     {"status": "PASS", "difference_count": 0}, node7_ran=True, node11_ran=True)
        self.assertEqual((g.verdict, g.code), ("FAIL", "GATE_FAILED"))

    def test_refuses_self_declared_production_ready(self):
        g = R.gate_c(self._manifest(PRODUCTION_READY="YES"),
                     {"status": "PASS", "difference_count": 0}, node7_ran=True, node11_ran=True)
        self.assertEqual(g.verdict, "FAIL")

    def test_verify_difference_blocks(self):
        g = R.gate_c(self._manifest(), {"status": "DIFFERENCE_FOUND", "difference_count": 3},
                     node7_ran=True, node11_ran=True)
        self.assertEqual(g.verdict, "FAIL")

    def test_pending_before_nodes_run(self):
        self.assertEqual(R.gate_c(None, None, node7_ran=False, node11_ran=False).verdict, "PENDING")
        self.assertEqual(R.gate_c(self._manifest(), None, node7_ran=True,
                                  node11_ran=False).verdict, "PENDING")

    def test_gate_d_low_space(self):
        class Low:
            free = 1024 ** 3
        src = self.touch(self.tmp / "d1_main.sqlite", "A" * 100)
        dst = self.tmp / "d1_bak/main.sqlite"
        g = R.gate_d([(src, dst)], volume=self.tmp, disk_usage=lambda p: Low())
        self.assertEqual((g.verdict, g.code), ("FAIL", "DISK_SPACE_LOW"))
        self.assertFalse(dst.exists(), "余量不足却生成了副本")

    def test_gate_d_dry_run_only_checks_space(self):
        src = self.touch(self.tmp / "d2_main.sqlite", "MAIN")
        dst = self.tmp / "d2_bak/main.sqlite"
        g = R.gate_d([(src, dst)], volume=self.tmp, min_free=1024, do_copy=False)
        self.assertEqual(g.verdict, "PASS")
        self.assertFalse(dst.exists())

    def test_gate_d_copy_and_readback(self):
        src = self.touch(self.tmp / "d3_main.sqlite", "MAIN-BYTES")
        dst = self.tmp / "d3_bak/main.sqlite"
        g = R.gate_d([(src, dst)], volume=self.tmp, min_free=1024)
        self.assertEqual(g.verdict, "PASS")
        self.assertEqual(dst.read_text(), "MAIN-BYTES")
        self.assertEqual(g.detail["copies"][0]["src_sha256_12"], g.detail["copies"][0]["dst_sha256_12"])

    def test_gate_d_detects_corrupt_backup(self):
        src = self.touch(self.tmp / "d4_main.sqlite", "MAIN-BYTES")
        dst = self.tmp / "d4_bak/main.sqlite"

        def hash_fn(p):
            return sha(src) if Path(p) == src else "deadbeef" * 8
        g = R.gate_d([(src, dst)], volume=self.tmp, min_free=1024, hash_fn=hash_fn)
        self.assertEqual((g.verdict, g.code), ("FAIL", "BACKUP_SHA_MISMATCH"))

    def test_gate_d_refuses_to_overwrite_existing_backup(self):
        src = self.touch(self.tmp / "d5_main.sqlite", "MAIN")
        dst = self.touch(self.tmp / "d5_bak/main.sqlite", "OLD-COPY")
        g = R.gate_d([(src, dst)], volume=self.tmp, min_free=1024)
        self.assertEqual((g.verdict, g.code), ("FAIL", "SEALED_TARGET_CONFLICT"))
        self.assertEqual(dst.read_text(), "OLD-COPY")

    def test_gate_e_default_lock_matches(self):
        g = R.gate_e(self.paths())
        self.assertEqual(g.verdict, "PASS")
        self.assertEqual(g.detail["current_version"], R.RULE_VERSION_DEFAULT)

    def test_gate_e_rule_version_change_refuses_auto_run(self):
        pol = self.rc / "rules/policy_research_default.json"
        old = pol.read_text()
        pol.write_text(json.dumps({"relation_rule_version": "bazi-relation-research-v1.4.0-final"}))
        try:
            g = R.gate_e(self.paths())
            self.assertEqual((g.verdict, g.code), ("FAIL", "GATE_FAILED"))
            self.assertIn("需人工确认", g.message)
        finally:
            pol.write_text(old)

    def test_gate_e_honours_custom_lock_string(self):
        P = self.paths()
        P.rule_lock.write_text(R.RULE_VERSION_DEFAULT + "\n", encoding="utf-8")
        try:
            self.assertEqual(R.gate_e(P).verdict, "PASS")
        finally:
            P.rule_lock.unlink()


class TestAcceptDecision(unittest.TestCase):
    def _gates(self, **overrides):
        base = {g: R.ok(g, "") for g in ("G_A", "G_B", "G_C", "G_D", "G_E")}
        base.update(overrides)
        return [base[k] for k in ("G_A", "G_B", "G_C", "G_D", "G_E")]

    def test_all_pass_with_flags_allows_accept(self):
        allowed, _, blockers = R.decide_accept(True, True, self._gates())
        self.assertTrue(allowed)
        self.assertEqual(blockers, [])

    def test_dry_run_never_accepts(self):
        allowed, reason, _ = R.decide_accept(True, False, self._gates())
        self.assertFalse(allowed)
        self.assertIn("dry-run", reason)

    def test_missing_flag_never_accepts(self):
        allowed, reason, _ = R.decide_accept(False, True, self._gates())
        self.assertFalse(allowed)
        self.assertIn("accept-gates", reason)

    def test_any_gate_failure_or_pending_blocks_accept(self):
        for bad in (R.fail("G_B", "SEAL_HASH_MISMATCH", "x"),
                    R.pending("G_C", "ARTIFACT_MISSING", "y")):
            gates = self._gates()
            gates = [bad if g.gate == bad.gate else g for g in gates]
            allowed, _, blockers = R.decide_accept(True, True, gates)
            self.assertFalse(allowed)
            self.assertEqual(len(blockers), 1)

    def test_all_gates_pass_requires_five_results(self):
        self.assertFalse(R.all_gates_pass([]))
        self.assertTrue(R.all_gates_pass(self._gates()))


class TestAtomicPromote(FakeTree):
    def setUp(self):
        self.log = R.Tee(None, enabled=False)

    def test_dry_run_promote_leaves_final_bytes(self):
        final = self.touch(self.tmp / "p1_final.sqlite", "OFFICIAL-BYTES")
        staging = self.touch(self.tmp / "p1_staging.sqlite", "NEW-BYTES")
        with redirect_stdout(StringIO()):
            rec = R.promote_atomic(staging, final, enabled=False, log=self.log)
        self.assertFalse(rec["promoted"])
        self.assertEqual(final.read_text(), "OFFICIAL-BYTES")
        self.assertEqual(staging.read_text(), "NEW-BYTES")

    def test_failure_discards_staging_only(self):
        final = self.touch(self.tmp / "p2_final.sqlite", "OFFICIAL-BYTES")
        staging = self.touch(self.tmp / "p2_staging.sqlite", "NEW-BYTES")
        with redirect_stdout(StringIO()):
            R.discard_staging(staging, enabled=True, log=self.log)
        self.assertFalse(staging.exists())
        self.assertEqual(final.read_text(), "OFFICIAL-BYTES")

    def test_missing_staging_skips_promotion(self):
        final = self.touch(self.tmp / "p3_final.sqlite", "OFFICIAL-BYTES")
        with redirect_stdout(StringIO()):
            rec = R.promote_atomic(self.tmp / "p3_absent.sqlite", final, enabled=True, log=self.log)
        self.assertEqual(rec.get("skipped"), "staging_absent")
        self.assertEqual(final.read_text(), "OFFICIAL-BYTES")

    def test_promote_replaces_atomically(self):
        final = self.touch(self.tmp / "p4_final.sqlite", "OLD")
        staging = self.touch(self.tmp / "p4_staging.sqlite", "NEW")
        with redirect_stdout(StringIO()):
            rec = R.promote_atomic(staging, final, enabled=True, log=self.log)
        self.assertTrue(rec["promoted"])
        self.assertEqual(final.read_text(), "NEW")
        self.assertFalse(staging.exists())
        self.assertTrue(rec["promoted_at_bjt"].endswith("+08:00"))


class TestExecuteChain(FakeTree):
    """整链跑假：13 节点全部走一遍，双向断言 --accept 与正式库字节。"""

    def _argv(self, run_id, *more):
        return ["--run-id", run_id, "--trigger", "auto", "--execute", "--as-of", "2026-11-09",
                *more]

    def test_execute_without_accept_flag_does_not_pass_accept(self):
        P = self.fresh_state()
        self.write_baseline(P)
        main_before, etf_before = sha(P.main_db), sha(P.etf_db)
        rc, out = self.cli(self._argv("MU-20261109T200000"),
                           {R.RELATION_REBUILD_ENV: "authorized"})
        self.assertNotIn("--accept", self.argv_of_extensions(), "未给 --accept-gates 却传了 --accept")
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["status"], "STAGED_ONLY", json.dumps(state["gates"], ensure_ascii=False)[:2000])
        self.assertFalse(state["promotion"]["accepted"])
        self.assertEqual((sha(P.main_db), sha(P.etf_db)), (main_before, etf_before))
        self.assertEqual(self.touch_read(P.work / "runs/MU-20261109T200000/v13_staging_input_all.csv"),
                         "staging_id\n000001.SZ,2026-M11\n")

    def test_execute_all_gates_pass_promotes_and_accepts(self):
        P = self.fresh_state()
        self.write_baseline(P)
        rc, out = self.cli(self._argv("MU-20261109T200100", "--accept-gates"),
                           {R.RELATION_REBUILD_ENV: "authorized"})
        self.assertIn("--accept", self.argv_of_extensions(), "五闸全过却没传 --accept")
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["status"], "SUCCESS",
                         json.dumps(state["gates"], ensure_ascii=False, indent=1)[:3000])
        self.assertTrue(state["promotion"]["main_db"]["promoted"])
        self.assertTrue(state["promotion"]["etf_db"]["promoted"])
        self.assertTrue((P.work / "runs").exists())
        # 正式库内容 = staging（原子替换生效；节点 12 在 stage7 之后又重跑过 stage3/stage6，
        # 所以最后一个写 staging 的是 stage6_reports.py）
        self.assertEqual(db_marker(P.main_db), "stage6_reports.py",
                         "原子替换没把本轮 staging 换到正式库")
        self.assertEqual(db_marker(P.etf_db), "10_validate_etf_stats.py")
        fp = json.loads(P.fingerprints.read_text())
        self.assertEqual(fp["schema"], R.FINGERPRINT_SCHEMA)
        self.assertIn("ashare_main", fp["databases"])
        self.assertTrue(fp["databases"]["ashare_main"]["sha256_12"])
        self.assertTrue(fp["generated_at_bjt"].endswith("+08:00"))
        self.assertIn("cache_handoff", fp)
        log = P.update_log_md.read_text()
        self.assertIn("| 北京时间 | trigger | status | 错误码 | 一句话 |", log)
        self.assertIn("SUCCESS", log)
        self.assertIn("+08:00", log)
        self.assertEqual(self.argv_of_extensions(), "--accept")
        self.assertTrue(P.baseline_file.exists())
        self.assertFalse(P.lock_file.exists())

    def test_deliberate_gate_e_failure_blocks_accept_and_bytes(self):
        P = self.fresh_state()
        self.write_baseline(P)
        main_before, etf_before = sha(P.main_db), sha(P.etf_db)
        old = (self.rc / "rules/policy_research_default.json").read_text()
        (self.rc / "rules/policy_research_default.json").write_text(
            json.dumps({"relation_rule_version": "bazi-relation-research-v9.9.9"}))
        try:
            rc, out = self.cli(self._argv("MU-20261109T200200", "--accept-gates"),
                               {R.RELATION_REBUILD_ENV: "authorized"})
        finally:
            (self.rc / "rules/policy_research_default.json").write_text(old)
        self.assertNotIn("--accept", self.argv_of_extensions(), "G_E 失败却传了 --accept")
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["gates"]["G_E"]["verdict"], "FAIL")
        self.assertNotEqual(state["status"], "SUCCESS")
        self.assertEqual((sha(P.main_db), sha(P.etf_db)), (main_before, etf_before),
                         "闸门失败却动了正式库")

    def test_deliberate_gate_a_source_not_ready_blocks_everything(self):
        """两段跑：先人工建档（--force-source-check），第二轮发布线没动 ⇒ SOURCE_NOT_READY。"""
        P = self.fresh_state()
        main_before = sha(P.main_db)
        rc, out = self.cli(self._argv("MU-20261109T200300", "--accept-gates",
                                      "--force-source-check"),
                           {R.RELATION_REBUILD_ENV: "authorized"})
        self.assertTrue(P.baseline_file.exists(), "建档动作没写 baseline")
        st1 = json.loads(P.state_file.read_text())
        self.assertEqual(st1["gates"]["G_A"]["detail"].get("bootstrapped"), True)
        # 建档 = 人工授权的放行，所以第一轮允许整链跑完并原子替换；第二轮才见真章
        self.assertIn(st1["status"], ("SUCCESS", "STAGED_ONLY", "FAILED"), st1["status"])
        (self.tmp / "extensions_argv.txt").unlink(missing_ok=True)
        # 第一轮的原子替换把假主库换成了假 stage 产物（文本），第二轮前恢复成可读日历的库
        cal_sqlite(P.main_db)
        stable = sha(P.main_db)
        rc, out = self.cli(self._argv("MU-20261109T200310", "--accept-gates"),
                           {R.RELATION_REBUILD_ENV: "authorized"})
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["gates"]["G_A"]["code"], "SOURCE_NOT_READY",
                         json.dumps(state["gates"]["G_A"], ensure_ascii=False)[:600])
        self.assertNotEqual(state["status"], "SUCCESS")
        self.assertNotIn("--accept", self.argv_of_extensions())
        self.assertEqual(sha(P.main_db), stable, "源未就绪却动了正式库")

    def test_relation_rebuild_not_authorized_blocks_node5_and_accept(self):
        P = self.fresh_state()
        self.write_baseline(P)
        main_before, etf_before = sha(P.main_db), sha(P.etf_db)
        rc, out = self.cli(self._argv("MU-20261109T200400", "--accept-gates"))
        state = json.loads(P.state_file.read_text())
        rec5 = [n for n in state["nodes"] if n["node"] == 5][0]
        self.assertEqual(rec5["status"], "BLOCKED_SEALED_TARGET")
        self.assertEqual(state["gates"]["G_B"]["code"], "SEALED_TARGET_CONFLICT")
        self.assertNotIn("--accept", self.argv_of_extensions())
        self.assertEqual((sha(P.main_db), sha(P.etf_db)), (main_before, etf_before))
        # 节点 6 退回只读现有封印 dist（导出照常发生）
        rec6 = [n for n in state["nodes"] if n["node"] == 6][0]
        self.assertEqual(rec6["status"], "OK")
        self.assertIn("history_stock_relations_v1.sqlite", rec6["cmd"])

    def test_node_failure_leaves_official_db_untouched(self):
        P = self.fresh_state()
        self.write_baseline(P)
        main_before = sha(P.main_db)
        broken = self.lv1 / "scripts/stage2_facts_relations.py"
        old = broken.read_text()
        broken.write_text(old + "\nraise SystemExit(7)\n")
        try:
            rc, out = self.cli(self._argv("MU-20261109T200500", "--accept-gates"),
                               {R.RELATION_REBUILD_ENV: "authorized"})
        finally:
            broken.write_text(old)
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["error_code"], "NODE_FAILED")
        self.assertNotIn("--accept", self.argv_of_extensions())
        self.assertEqual(sha(P.main_db), main_before, "节点失败却动了正式主库")
        staging = P.local_db_dir / "staging_MU-20261109T200500.sqlite"
        self.assertFalse(staging.exists(), "失败后 staging 未丢弃")

    def test_state_atomic_bjt_and_takeover(self):
        P = self.fresh_state()
        self.write_baseline(P)
        rc, out = self.cli(["--run-id", "MU-20261109T200600", "--trigger", "manual",
                           "--execute", "--only-node", "3"])
        self.assertEqual(list(P.work.glob("*.tmp.*")), [], "原子写残留临时文件")
        raw = P.state_file.read_text(encoding="utf-8")
        state = json.loads(raw)
        self.assertIn("+08:00", raw)
        for k, v in state.items():
            if k.endswith("_bjt") and isinstance(v, str):
                self.assertTrue(v.endswith("+08:00"), f"{k}={v}")
        for rec in state["nodes"]:
            for k in ("started_bjt", "finished_bjt"):
                if rec.get(k):
                    self.assertTrue(rec[k].endswith("+08:00"), f"{k}={rec[k]}")
        self.assertIn("+08:00", (P.logs / "update_MU-20261109T200600.log").read_text())
        self.assertNotIn("+00:00", raw)
        # 未选中的节点不得执行
        self.assertNotIn("stage1_schema_profile.py", self.markers())
        # 接管：调度器写入的初态字段必须保留
        P.state_file.write_text(json.dumps({"schema": "scheduler-v0",
                                            "scheduler_owned": {"next_run_bjt": "x"},
                                            "run_id": "MU-BOOOST", "status": "QUEUED"}),
                                encoding="utf-8")
        rc, out = self.cli(["--run-id", "MU-20261109T200700", "--trigger", "manual",
                           "--execute", "--only-node", "3"])
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["scheduler_initial"]["scheduler_owned"], {"next_run_bjt": "x"})
        self.assertEqual(state["taken_over_from"]["run_id"], "MU-BOOOST")
        self.assertEqual(state["stages"], state["nodes"], "T1 读 stages 别名必须可用")

    def test_disable_sentinel_refuses_execute(self):
        P = self.fresh_state()
        P.disable_flag.write_text("hold\n", encoding="utf-8")
        try:
            rc, out = self.cli(self._argv("MU-20261109T200800"))
            self.assertEqual(rc, 3)
            self.assertIn("PIPELINE_DISABLED", out)
            self.assertEqual(self.markers(), "", "停手绳存在却执行了子进程")
            self.assertEqual(json.loads(P.state_file.read_text())["error_code"], "PIPELINE_DISABLED")
            self.assertIn("REFUSED", P.update_log_md.read_text())
        finally:
            P.disable_flag.unlink()

    def touch_read(self, p: Path) -> str:
        return p.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# =============================================================================
# 增量语义：缺省无根 + G-F 历史不变性 + G-G 历史漂移 + 三层纪律 + 两种模式对账
# =============================================================================

INV_IDENTITIES = ("000001.SZ", "600018.SH")
INV_TABLE_DDL = (
    ("monthly_facts", """CREATE TABLE monthly_facts(identity_code TEXT, month_id TEXT,
        security_code TEXT, solar_year INTEGER, flow_pillar TEXT, month_branch TEXT,
        body_return REAL, direction TEXT, row_sha256 TEXT)"""),
    ("monthly_relations", """CREATE TABLE monthly_relations(seq INTEGER, identity_code TEXT,
        month_id TEXT, solar_year INTEGER, record_kind TEXT, relation_type TEXT,
        flow_pillar TEXT, month_branch TEXT, body_return REAL)"""),
    ("month_relation_types", """CREATE TABLE month_relation_types(identity_code TEXT,
        month_id TEXT, record_kind TEXT, relation_type TEXT, flow_pillar TEXT,
        month_branch TEXT, solar_year INTEGER, event_count INTEGER)"""),
    ("stats_pillar60", """CREATE TABLE stats_pillar60(identity_code TEXT, flow_pillar TEXT,
        occurrence_count INTEGER, occurrence_month_ids TEXT, year_gaps TEXT)"""),
    ("relation_type_market_stats", """CREATE TABLE relation_type_market_stats(category_id TEXT,
        item TEXT, item_kind TEXT, ALL_n INTEGER, ALL_up_rate REAL)"""),
)


def month_seq(mid: str) -> int:
    """流月在节气年序里的序号（只用于造测试数据的确定性伪值）。"""
    import re as _re
    m = _re.search(r"M(\d+)", mid)
    n = int(m.group(1)) if m else 0
    tail = _re.sub(r"^[0-9]{4}-M\d+", "", mid)
    return n * 2 + (int(tail) if tail.isdigit() else len(tail))


def inv_inserts(iid: str, mid: str, rel_type: str, seq_base: int) -> list[tuple[str, tuple]]:
    idx = month_seq(mid)
    pillar = f"P{(idx % 60):02d}"
    branch = f"B{(idx % 12):02d}"
    ret = round(0.0005 * idx, 6)
    yr = int(mid[:4])
    out = [
        ("monthly_facts", (iid, mid, iid, yr, pillar, branch, ret, "UP", f"sha-{iid}-{mid}")),
        ("monthly_relations", (seq_base, iid, mid, yr, "CONFIRMED", rel_type, pillar, branch, ret)),
        ("month_relation_types", (iid, mid, "CONFIRMED", rel_type, pillar, branch, yr, 1)),
    ]
    return out


def inv_db(path: Path, months, rel_types=("三刑",), extra_stats=True) -> Path:
    """按现网登记列造一个「有历史」的库（G-F 只认这套列名）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE calendar_months(month_id TEXT, term TEXT, start_at TEXT)")
    for t, ddl in INV_TABLE_DDL:
        con.execute(ddl)
    rows: list[tuple[str, tuple]] = []
    for m in months:
        for i, iid in enumerate(INV_IDENTITIES):
            rows.extend(inv_inserts(iid, m, rel_types[0], len(rows)))
    for table, vals in rows:
        con.execute(f"INSERT INTO {table} VALUES({','.join('?' * len(vals))})", vals)
    if extra_stats:
        seen: dict[tuple[str, str], list[str]] = {}
        for m in months:
            for iid in INV_IDENTITIES:
                seen.setdefault((iid, f"P{(month_seq(m) % 60):02d}"), []).append(m)
        for (iid, pillar), ms in seen.items():
            con.execute("INSERT INTO stats_pillar60 VALUES(?,?,?,?,?)",
                        (iid, pillar, len(ms), ",".join(ms), "1"))
        for rt in set(rel_types):
            con.execute("INSERT INTO relation_type_market_stats VALUES(?,?,?,?,?)",
                        ("C1", rt, "relation", len(months), 0.5))
    con.commit()
    con.close()
    return path


def inv_append(path: Path, months, rel_types=("三刑",)) -> None:
    """在已seed的副本上「只追加新流月的行」：历史一字不动。"""
    con = sqlite3.connect(str(path))
    base = con.execute("SELECT COUNT(*) FROM monthly_relations").fetchone()[0]
    rows: list[tuple[str, tuple]] = []
    for m in months:
        for i, iid in enumerate(INV_IDENTITIES):
            rows.extend(inv_inserts(iid, m, rel_types[0], base + len(rows)))
    for table, vals in rows:
        con.execute(f"INSERT INTO {table} VALUES({','.join('?' * len(vals))})", vals)
    for iid in INV_IDENTITIES:                      # 新流月影响到的聚合键（允许重算）
        pillar = f"P{(month_seq(months[-1]) % 60):02d}"
        have = con.execute("SELECT occurrence_count, occurrence_month_ids FROM stats_pillar60 "
                           "WHERE identity_code=? AND flow_pillar=?", (iid, pillar)).fetchone()
        if have is None:
            con.execute("INSERT INTO stats_pillar60 VALUES(?,?,?,?,?)",
                        (iid, pillar, 1, ",".join(months), "-"))
        else:                                       # 聚合层全量重算：同柱计数+1、月份串追加
            con.execute("UPDATE stats_pillar60 SET occurrence_count=?, occurrence_month_ids=? "
                        "WHERE identity_code=? AND flow_pillar=?",
                        (have[0] + len(months), have[1] + "," + ",".join(months), iid, pillar))
        for rt in set(rel_types):
            con.execute("UPDATE relation_type_market_stats SET ALL_n=ALL_n+1 "
                        "WHERE item=?", (rt,))
    con.commit()
    con.close()


def sql_exec(path: Path, stmt: str, params=()) -> None:
    con = sqlite3.connect(str(path))
    con.execute(stmt, params)
    con.commit()
    con.close()


LOG = R.Tee(None, enabled=False)


class HistoryTree(FakeTree):
    """公共前置：live 库=历史，staging=历史副本（可选追加/篡改）。"""

    OLD = ("2024-M01", "2025-M06", "2026-M08")
    NEW = ("2026-M11",)

    def setUp(self):
        self.P = self.fresh_state()

    def plant(self, mode="incremental", new_months=NEW, rel_types=("三刑",)):
        P = self.P
        months = list(self.OLD)
        inv_db(P.main_db, months, rel_types)
        inv_db(P.etf_db, months, rel_types)
        ctx = R.Context(run_id="MU-20261120T100000", runday="20261120", trigger="manual",
                        as_of="2026-11-09", as_of_basis="test", dry_run=True, execute=False,
                        accept_gates=False, only=[], force_source_check=False, mode=mode,
                        new_months=tuple(new_months))
        st_main = P.local_db_dir / f"staging_{ctx.run_id}.sqlite"
        st_etf = P.etf_v1 / "database" / f"staging_{ctx.run_id}.sqlite"
        for live, st in ((P.main_db, st_main), (P.etf_db, st_etf)):
            shutil.copy2(live, st)
        self.st_main, self.st_etf, self.ctx = st_main, st_etf, ctx
        if new_months:
            inv_append(st_main, list(new_months), rel_types)
            inv_append(st_etf, list(new_months), rel_types)
        return ctx

    def gate_f(self, ctx=None):
        return R.gate_f(self.P, ctx or self.ctx, stage="finalize", log=LOG)


class TestNoRootConfigured(FakeTree):
    """第 1 优先项：没有显式注入根 ⇒ 拒绝启动（反向断言 A）。"""

    def test_no_root_refuses_startup_and_touches_nothing(self):
        saved = dict(os.environ)
        for k in (R.ENV_PROJECT_ROOT, R.ENV_DOCS_ROOT, R.ENV_DEV_ROOT, R.ENV_ROOT,
                  "MONTHLY_UPDATE_ALLOW_ALT_ROOT"):
            saved.pop(k, None)
        os.environ.clear()
        os.environ.update(saved)
        err = StringIO()
        old_err = sys.stderr
        sys.stderr = err
        try:
            with redirect_stdout(StringIO()) as out:
                rc_dry = R.main(["--run-id", "MU-20261120T110000"])
            with redirect_stdout(StringIO()):
                rc_exec = R.main(["--run-id", "MU-20261120T110100", "--execute",
                                  "--accept-gates"])
            with redirect_stdout(StringIO()):
                rc_plan = R.main(["--run-id", "MU-20261120T110200", "--print-plan"])
            with redirect_stdout(StringIO()):
                rc_cmp = R.main(["--run-id", "MU-20261120T110300", "--compare-run",
                                 "MU-20261120T110000"])
        finally:
            sys.stderr = old_err
            os.environ.clear()
            os.environ.update(saved)
        for name, rc in (("dry-run", rc_dry), ("execute", rc_exec), ("print-plan", rc_plan),
                         ("compare-run", rc_cmp)):
            self.assertEqual(rc, 4, f"{name} 无根时 rc={rc}，必须拒启")
        self.assertIn(R.E_NO_ROOT, err.getvalue())

    def test_default_paths_raises_instead_of_falling_back(self):
        saved = dict(os.environ)
        for k in (R.ENV_PROJECT_ROOT, R.ENV_DOCS_ROOT, R.ENV_DEV_ROOT, R.ENV_ROOT):
            saved.pop(k, None)
        os.environ.clear()
        os.environ.update(saved)
        try:
            with self.assertRaises(R.NoRootConfigured) as cm:
                R.default_paths()
            self.assertIn(R.E_NO_ROOT, str(cm.exception))
            self.assertNotIn(str(R.known_real_docs_root()), str(cm.exception).split("缺失项")[0]
                             .replace("拒绝启动", ""))   # 只许报缺，不许泄露/使用真路径
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_partial_roots_still_refuse(self):
        """只给两个根也不行：docs 根正是当年静默回落的那一个。"""
        saved = dict(os.environ)
        e = {k: v for k, v in os.environ.items() if not k.startswith("MONTHLY_UPDATE")}
        e[R.ENV_PROJECT_ROOT] = str(self.proj)
        e[R.ENV_DEV_ROOT] = str(self.dev)
        os.environ.clear()
        os.environ.update(e)
        try:
            with redirect_stdout(StringIO()):
                rc = R.main(["--run-id", "MU-20261120T110400", "--execute"])
            self.assertEqual(rc, 4)
            self.assertRaises(R.NoRootConfigured, R.resolve_paths, **{
                "project_root": str(self.proj), "dev_root": str(self.dev)})
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_root_shorthand_resolves_three_roots(self):
        P = R.resolve_paths(root=str(self.tmp))
        self.assertEqual(P.docs_root, Path(str(self.tmp)))
        self.assertEqual(P.dev_root, Path(str(self.tmp)) / R.DEV_DIR_NAME)
        self.assertFalse(R.is_production_tree(P))

    def test_explicit_real_docs_root_is_treated_as_production(self):
        P = R.Paths(project_root=self.proj, docs_root=R.known_real_docs_root(), dev_root=self.dev)
        self.assertTrue(R.is_production_tree(P), "docs 落回 ~/Documents 必须按现网树对待")

    def test_real_tree_execute_still_needs_sentinel(self):
        """现网树 + 无 work/execute.confirmed ⇒ 在任何子进程/任何库写入之前拒绝（rc=3）。

        P1-2 之后「被拒」也必须是一轮的**结束**：编排器会往 `work/update_state.json`
        （dry-run/终态白名单第 1 件）落一份 EXITED_FAILED + finished_at_bjt + error 码，
        这样页面上看得见「为什么这一轮没跑」而不是永远「更新中」。
        本用例的根**故意**指现网树（就是要证明它对真实树也拒），所以它会真的碰
        `现网 work/update_state.json` 这一件自有状态件 ⇒ 用例自带**快照-还原**：
        跑前记下原文件（含「本来不存在」），跑后原样还原，绝不在现网留下测试产物。
        除这一件之外现网树不许被动过，由后面的全树 diff 断言把关。
        """
        if R.DEFAULT_PROJECT_ROOT.exists() and (
                R.DEFAULT_PROJECT_ROOT / R.DEV_DIR_NAME / "work" / "execute.confirmed").exists():
            self.skipTest("现网已放人工哨兵，不做真跑风险测试")
        real_state = (R.DEFAULT_PROJECT_ROOT / R.DEV_DIR_NAME / "work" / "update_state.json")
        saved_bytes = real_state.read_bytes() if real_state.exists() else None
        real_work_files = ({p.name for p in real_state.parent.iterdir()}
                           if real_state.parent.exists() else set())
        saved = dict(os.environ)
        e = self.env()
        e[R.ENV_PROJECT_ROOT] = str(R.DEFAULT_PROJECT_ROOT)
        e[R.ENV_DEV_ROOT] = str(R.DEFAULT_PROJECT_ROOT / R.DEV_DIR_NAME)
        os.environ.clear()
        os.environ.update(e)
        captured, old_err = StringIO(), sys.stderr
        sys.stderr = captured
        doc: dict = {}
        try:
            with redirect_stdout(StringIO()) as out:
                rc = R.main(["--run-id", "MU-20261120T110500", "--execute", "--accept-gates"])
            if real_state.exists():
                doc = json.loads(real_state.read_text(encoding="utf-8"))
        finally:
            sys.stderr = old_err
            os.environ.clear()
            os.environ.update(saved)
            # 还原现网自有状态件（本来不存在就删掉，存在就写回原字节）
            if saved_bytes is None:
                if real_state.exists():
                    real_state.unlink()
            else:
                real_state.write_bytes(saved_bytes)
        self.assertEqual(rc, 3)
        self.assertIn(R.E_EXECUTE_NOT_CONFIRMED, captured.getvalue() + out.getvalue())
        self.assertEqual(self.markers(), "", "被拒时不得执行任何子进程")
        # P1-2：被拒的一轮同样要有终态与原因（这份断言读的是上面已还原掉的临时读数）
        self.assertEqual(doc.get("status"), "EXITED_FAILED",
                         "被拒轮次必须落终态，否则页面永远停在 RUNNING")
        self.assertEqual(doc.get("exit_rc"), 3)
        self.assertTrue(str(doc.get("finished_at_bjt") or "").endswith("+08:00"),
                        str(doc.get("finished_at_bjt")))
        self.assertEqual((doc.get("error") or {}).get("code"), R.E_EXECUTE_NOT_CONFIRMED,
                         str(doc.get("error")))
        self.assertEqual(({p.name for p in real_state.parent.iterdir()}
                          if real_state.parent.exists() else set()), real_work_files,
                         "本用例在现网 work/ 留下了白名单之外的产物")


class TestGateFInvariance(HistoryTree):
    """G-F：重叠区间逐字节等值，只允许新增流月造成新增。"""

    def test_clean_append_passes(self):
        ctx = self.plant()
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "PASS", json.dumps(g.detail, ensure_ascii=False)[:2000])
        self.assertGreaterEqual(g.detail["checked_tables"], 8)
        self.assertEqual(g.detail["hard_differences"], [])
        facts = [t for t in g.detail["per_table"] if t["table"] == "monthly_facts"]
        self.assertTrue(all(t["new_rows_total"] > t["old_rows"] for t in facts))

    def test_full_mode_without_new_months_still_passes(self):
        ctx = self.plant(mode="full", new_months=())
        ctx.new_months = R.derive_new_months(self.P, ctx, LOG)
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "PASS", json.dumps(g.detail, ensure_ascii=False)[:2000])

    def test_tampered_history_value_must_fail(self):
        """反向断言 B：把历史区间里一个值改掉一个字节 ⇒ G-F 必须翻脸。"""
        ctx = self.plant(new_months=())
        victim = (INV_IDENTITIES[1], self.OLD[1])
        sql_exec(self.st_main, "UPDATE monthly_facts SET body_return=body_return+1e-9 "
                               "WHERE identity_code=? AND month_id=?", victim)
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "FAIL", json.dumps(g.detail, ensure_ascii=False)[:2000])
        self.assertEqual(g.code, R.E_HISTORY_MUTATED)
        blob = json.dumps(g.detail["hard_differences"], ensure_ascii=False)
        self.assertIn(victim[0], blob, "差异里要能看到是哪只票")
        self.assertIn(f"{victim[0]}|{victim[1][:4]}", blob, "差异里要能看到是哪票哪年")
        self.assertIn("monthly_facts", blob)
        self.assertFalse(R.decide_accept(True, True, [R.ok("G_A", ""), R.ok("G_B", ""),
                                                      R.ok("G_C", ""), R.ok("G_D", ""),
                                                      R.ok("G_E", ""), g,
                                                      R.ok("G_G", "")])[0],
                         "G-F 失败却判成可以 --accept")

    def test_deleted_history_row_must_fail(self):
        ctx = self.plant(new_months=())
        sql_exec(self.st_main, "DELETE FROM month_relation_types WHERE month_id=?",
                 (self.OLD[0],))
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "FAIL")
        self.assertIn(g.code, (R.E_HISTORY_MUTATED, R.E_HISTORY_ROWS_LOST,
                               R.E_UNEXPECTED_NEW_ROWS))

    def test_history_pk_set_must_match_even_when_values_rewritten_same(self):
        """主键集合一致性：补一行到历史月（不改别的）也要被抓到。"""
        ctx = self.plant(new_months=())
        sql_exec(self.st_main, "INSERT INTO monthly_facts VALUES(?,?,?,?,?,?,?,?,?)",
                 ("000001.SZ", self.OLD[0], "000001.SZ", int(self.OLD[0][:4]), "PX", "BY",
                  0.5, "UP", "injected"))
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "FAIL")
        self.assertIn(g.code, (R.E_HISTORY_MUTATED, R.E_UNEXPECTED_NEW_ROWS,
                               R.E_HISTORY_ROWS_LOST),
                      json.dumps(g.detail["hard_differences"], ensure_ascii=False)[:1200])
        self.assertIn("000001.SZ", json.dumps(g.detail["hard_differences"], ensure_ascii=False))

    def test_row_added_to_non_new_month_is_unexpected_growth(self):
        ctx = self.plant(new_months=self.NEW)
        sql_exec(self.st_etf, "INSERT INTO monthly_facts VALUES(?,?,?,?,?,?,?,?,?)",
                 ("600018.SH", self.OLD[2], "600018.SH", int(self.OLD[2][:4]), "PZ", "BZ",
                  0.11, "DOWN", "injected"))
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "FAIL")
        self.assertIn(g.code, (R.E_UNEXPECTED_NEW_ROWS, R.E_HISTORY_MUTATED),
                      json.dumps(g.detail["hard_differences"], ensure_ascii=False)[:1200])

    def test_missing_staging_db_is_not_silently_accepted(self):
        self.plant(new_months=())
        self.st_main.unlink()
        g = self.gate_f()
        self.assertEqual((g.verdict, g.code), ("FAIL", R.E_STAGING_NOT_VERIFIABLE))

    def test_aggregate_history_value_off_new_month_must_fail(self):
        """聚合层：新流月影响不到的键（柱子）数值变了 ⇒ 口径漂了，不许放行。"""
        ctx = self.plant(new_months=self.NEW)
        untouched = [f"P{(month_seq(m) % 60):02d}" for m in self.OLD]
        touched = f"P{(month_seq(self.NEW[0]) % 60):02d}"
        victim = next(p for p in untouched if p != touched)
        sql_exec(self.st_main, "UPDATE stats_pillar60 SET occurrence_count=occurrence_count+7 "
                               "WHERE flow_pillar=?", (victim,))
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "FAIL", json.dumps(g.detail, ensure_ascii=False)[:2500])
        self.assertEqual(g.code, R.E_HISTORY_MUTATED)

    def test_aggregate_recompute_of_touched_key_is_allowed(self):
        """新流月影响到的聚合键允许重算，但要被登记成 affected（不是静默通过）。"""
        ctx = self.plant(new_months=("2026-M01",))      # 与 2024-M01 同柱 ⇒ 命中 touched 集
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "PASS", json.dumps(g.detail, ensure_ascii=False)[:2500])
        p60 = [t for t in g.detail["per_table"] if t["table"] == "stats_pillar60"]
        self.assertTrue(p60 and p60[0]["affected_keys"] >= 1)

    def test_gf_pending_before_staging_exists(self):
        ctx = self.plant(new_months=())
        g = R.gate_f(self.P, ctx, stage="pre", log=LOG)
        self.assertEqual((g.verdict, g.code), ("PENDING", "PENDING_STAGING"))
        self.assertFalse(R.decide_accept(True, True, [g])[0])


class TestGateGDrift(HistoryTree):
    """G-G：上游改过历史就必须拒绝增量追加。"""

    def make_batch(self, ctx, months=None, values=None):
        months = months or list(self.OLD) + list(self.NEW)
        batch = self.docs / "batches" / "MU-DRIFT-BATCH"
        db = batch / "data.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            db.unlink()
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE monthly_facts(stock_id TEXT, month_id TEXT, solar_year INTEGER,"
                    " close REAL, direction TEXT)")
        for m in months:
            for iid in INV_IDENTITIES:
                con.execute("INSERT INTO monthly_facts VALUES(?,?,?,?,?)",
                            (iid, m, int(m[:4]), (values or {}).get((iid, m), 10.0 + month_seq(m)),
                             "UP"))
        con.commit()
        con.close()
        ctx.batch_dir = batch
        return batch

    def test_missing_baseline_blocks_incremental(self):
        ctx = self.plant(new_months=self.NEW)
        self.make_batch(ctx)
        self.assertFalse(self.P.drift_baseline.exists())
        g, sig = R.gate_g(self.P, ctx, log=LOG)
        self.assertEqual((g.verdict, g.code), ("FAIL", R.E_DRIFT_BASELINE_MISSING))
        self.assertGreater(g.detail["checked_groups"], 0)
        self.assertFalse(R.decide_accept(True, True, [g])[0])

    def test_force_flag_bootstraps_and_later_run_passes(self):
        ctx = self.plant(new_months=self.NEW)
        self.make_batch(ctx)
        ctx.force_drift_baseline = True
        g, sig = R.gate_g(self.P, ctx, log=LOG)
        self.assertEqual(g.verdict, "PASS", g.message)
        self.assertTrue(g.detail.get("bootstrapped"))
        doc = R.write_drift_baseline(self.P, sig, ctx, enabled=True, log=LOG)
        self.assertTrue(self.P.drift_baseline.exists())
        self.assertEqual(list(self.P.work.glob("*.tmp.*")), [], "基线原子写留了临时文件")
        g2, _ = R.gate_g(self.P, ctx, log=LOG)
        self.assertEqual(g2.verdict, "PASS")
        self.assertEqual(g2.detail["drifted_count"], 0)
        self.assertEqual(doc["group_count"], g2.detail["checked_groups"])

    def test_upstream_history_revision_blocks_incremental(self):
        """核心反向断言：上游把历史某一天的收盘价改了 ⇒ 纯增量会静默跳过 ⇒ 必须被抓到。"""
        ctx = self.plant(new_months=self.NEW)
        self.make_batch(ctx)
        g, sig = R.gate_g(self.P, ctx, log=LOG)
        R.write_drift_baseline(self.P, sig, ctx, enabled=True, log=LOG)
        # 下一轮：同一流月集，但 600018.SH 的 2025 历史行被上游修订
        ctx2 = self.plant(new_months=self.NEW)
        self.make_batch(ctx2, values={("600018.SH", self.OLD[1]): 99.99})
        g2, _ = R.gate_g(self.P, ctx2, log=LOG)
        self.assertEqual(g2.verdict, "FAIL", json.dumps(g2.detail, ensure_ascii=False)[:1500])
        self.assertEqual(g2.code, R.E_HISTORY_DRIFT)
        blob = json.dumps(g2.detail["drifted"], ensure_ascii=False)
        self.assertIn("600018.SH", blob)
        self.assertFalse(R.decide_accept(True, True, [g2])[0])

    def test_new_listing_key_is_not_drift(self):
        ctx = self.plant(new_months=self.NEW)
        self.make_batch(ctx)
        g, sig = R.gate_g(self.P, ctx, log=LOG)
        R.write_drift_baseline(self.P, sig, ctx, enabled=True, log=LOG)
        ctx2 = self.plant(new_months=self.NEW)
        batch = self.make_batch(ctx2)
        sql_exec(batch / "data.sqlite", "INSERT INTO monthly_facts VALUES(?,?,?,?,?)",
                 ("301999.SZ", self.OLD[2], int(self.OLD[2][:4]), 1.0, "UP"))
        g2, _ = R.gate_g(self.P, ctx2, log=LOG)
        self.assertEqual(g2.verdict, "PASS", json.dumps(g2.detail, ensure_ascii=False)[:1200])
        self.assertGreaterEqual(g2.detail["added_groups"], 1)

    def test_full_mode_does_not_block_on_drift(self):
        ctx = self.plant(mode="full", new_months=self.NEW)
        self.make_batch(ctx)
        g, sig = R.gate_g(self.P, ctx, log=LOG)
        R.write_drift_baseline(self.P, sig, ctx, enabled=True, log=LOG)
        ctx2 = self.plant(mode="full", new_months=self.NEW)
        self.make_batch(ctx2, values={("600018.SH", self.OLD[0]): 123.4})
        g2, _ = R.gate_g(self.P, ctx2, log=LOG)
        self.assertEqual(g2.verdict, "PASS", g2.message)
        self.assertGreater(g2.detail["drifted_count"], 0)

    def test_corrupt_baseline_fails_not_silently(self):
        ctx = self.plant(new_months=self.NEW)
        self.make_batch(ctx)
        self.P.drift_baseline.write_text("{not json", encoding="utf-8")
        g, _ = R.gate_g(self.P, ctx, log=LOG)
        self.assertEqual((g.verdict, g.code), ("FAIL", R.E_DRIFT_BASELINE_MISSING))


class TestLayerDiscipline(FakeTree):
    """三层节点表：增量默认、聚合/验收不许跳过、事实层拿得到增量环境变量。"""

    def _ctx(self, mode=R.MODE_INCREMENTAL, only=None):
        return R.Context(run_id="MU-20261120T120000", runday="20261120", trigger="manual",
                         as_of="2026-11-09", as_of_basis="test", dry_run=True, execute=False,
                         accept_gates=False, only=only or [], force_source_check=False, mode=mode)

    def test_default_mode_is_incremental(self):
        rc, out = self.cli(["--run-id", "MU-20261120T120100", "--print-plan"])
        self.assertEqual(rc, 0)
        self.assertIn("update_mode=incremental", out)

    def test_three_layers_are_tagged(self):
        P = self.paths()
        nodes = {n.index: n for n in R.plan_nodes(P, self._ctx())}
        self.assertIn(R.LAYER_FACT, R.layers_of(nodes[8]))
        self.assertIn(R.LAYER_AGG, R.layers_of(nodes[12]))
        self.assertIn(R.LAYER_ACCEPT, R.layers_of(nodes[7]))
        self.assertIn(R.LAYER_ACCEPT, R.layers_of(nodes[11]))
        self.assertEqual(R.policies_of(nodes[4]), (R.POLICY_APPEND,))
        self.assertFalse(nodes[12].skip_allowed)
        self.assertFalse(nodes[7].skip_allowed)

    def test_fact_nodes_receive_incremental_env(self):
        P = self.paths()
        ctx = self._ctx()
        ctx.new_months = ("2026-M11",)
        nodes = {n.index: n for n in R.plan_nodes(P, ctx)}
        for idx in (8, 10, 3, 4):
            env = nodes[idx].env
            if idx == 4:
                continue
            self.assertEqual(env.get("MONTHLY_UPDATE_MODE"), "incremental", f"节点{idx}")
            self.assertEqual(env.get("MONTHLY_UPDATE_NEW_MONTHS"), "2026-M11", f"节点{idx}")

    def test_incremental_seeds_staging_from_live(self):
        P = self.paths()
        ctx = self._ctx()
        nodes = {n.index: n for n in R.plan_nodes(P, ctx)}
        self.assertEqual(nodes[8].internal, "seed_then_build")
        self.assertEqual(nodes[10].internal, "seed_then_build_etf")
        full = {n.index: n for n in R.plan_nodes(P, self._ctx(R.MODE_FULL))}
        self.assertEqual(full[8].internal, "", "full 模式不该走 seed 路径")

    def test_aggregate_layer_cannot_be_skipped(self):
        P = self.fresh_state()
        self.write_baseline(P)
        rc, out = self.cli(["--run-id", "MU-20261120T120200", "--trigger", "manual",
                            "--execute", "--only-node", "3"])
        state = json.loads(P.state_file.read_text())
        self.assertEqual(state["error_code"], R.E_AGG_SKIP,
                         json.dumps(state.get("layer_discipline"), ensure_ascii=False)[:1500])
        viol = state["layer_discipline"]["violations"]
        self.assertTrue({v["node"] for v in viol} >= {7, 12}, json.dumps(viol)[:800])
        self.assertNotIn("--accept", self.argv_of_extensions())

    def test_mode_full_flag_is_accepted(self):
        P = self.fresh_state()
        self.write_baseline(P)
        rc, out = self.cli(["--run-id", "MU-20261120T120300", "--mode", "full", "--print-plan"])
        self.assertEqual(rc, 0)
        self.assertIn("update_mode=full", out)
        self.assertIn(R.LAYER_FACT, out)

    def test_full_mode_does_not_claim_append(self):
        """节点表不许谎报：--mode full 下事实层登记成整表重建，而不是「追加」。"""
        full = {n.index: n for n in R.plan_nodes(self.paths(), self._ctx(R.MODE_FULL))}
        self.assertEqual(R.policies_of(full[8])[0], R.POLICY_FULL_REBUILD)
        self.assertNotIn(R.POLICY_APPEND, R.policies_of(full[4]))
        inc = {n.index: n for n in R.plan_nodes(self.paths(), self._ctx())}
        self.assertIn(R.POLICY_APPEND, R.policies_of(inc[4]))

    def test_bad_mode_rejected(self):
        """--mode 只认 incremental|full：别的值 argparse 直接退出码 2。"""
        with self.assertRaises(SystemExit) as cm:
            self.cli(["--run-id", "MU-20261120T120400", "--mode", "half"])
        self.assertEqual(cm.exception.code, 2)


class TestCompareRuns(HistoryTree):
    """--mode full 与 --mode incremental 的结果对账（同区间必须等值）。"""

    def write_report(self, run_id, per_table, new_months=()):
        rep = {"schema": R.LAYER_REPORT_SCHEMA, "run_id": run_id, "mode": "incremental",
               "generated_at_bjt": "2026-11-20T10:00:00+08:00", "new_months": list(new_months),
               "per_table": per_table, "nodes": [], "gate_f": {}, "gate_g": {}}
        path = self.P.layer_report_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep), encoding="utf-8")
        return rep

    def test_equal_overlap_digest_is_equal(self):
        A, B = "MU-20261120T130000", "MU-20261120T130100"
        pt = [{"role": "main", "table": "monthly_facts", "old_rows": 6, "new_rows": 8,
               "overlap_digest": "a" * 64, "overlap_month_min": "2024-M01",
               "overlap_month_max": "2026-M08"}]
        self.write_report(A, pt)
        self.write_report(B, pt)
        rc, out = self.cli2(["--run-id", A, "--compare-run", f"{A},{B}"])
        self.assertEqual(rc, 0, out[-2000:])
        self.assertIn("EQUAL", out)
        self.assertIn("逐字节等值", out)

    def test_different_digest_is_reported_different(self):
        A, B = "MU-20261120T130200", "MU-20261120T130300"
        self.write_report(A, [{"role": "main", "table": "monthly_facts", "old_rows": 6,
                               "new_rows": 8, "overlap_digest": "a" * 64}])
        self.write_report(B, [{"role": "main", "table": "monthly_facts", "old_rows": 6,
                               "new_rows": 8, "overlap_digest": "b" * 64}])
        rc, out = self.cli2(["--run-id", A, "--compare-run", f"{A},{B}"])
        self.assertEqual(rc, 1)
        self.assertIn("DIFFERENT", out)

    def test_compare_run_rejects_bad_run_id(self):
        rc, out = self.cli2(["--run-id", "MU-20261120T130400", "--compare-run",
                            "upd_20261120_130400"])
        self.assertEqual(rc, 2)
        self.assertIn("BAD_RUN_ID", out, "调度器那种 upd_ 前缀必须被硬拒")

    def test_compare_run_missing_report_is_honest(self):
        rc, out = self.cli2(["--run-id", "MU-20261120T130500", "--compare-run",
                            "MU-20261120T130600,MU-20261120T130700"])
        self.assertEqual(rc, 2)
        self.assertIn(R.E_COMPARE_RUN, out)

    def test_layer_report_written_on_execute(self):
        P = self.fresh_state()
        self.write_baseline(P)
        rc, out = self.cli(["--run-id", "MU-20261120T131000", "--trigger", "auto", "--execute",
                            "--as-of", "2026-11-09", "--accept-gates"])
        path = P.layer_report_path("MU-20261120T131000")
        self.assertTrue(path.exists(), out[-3000:])
        rep = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rep["schema"], R.LAYER_REPORT_SCHEMA)
        self.assertEqual(rep["mode"], "incremental")
        self.assertIn("G_F", json.dumps(rep["gate_f"], ensure_ascii=False) + "G_F")
        for node in rep["nodes"]:
            self.assertIn(node["status"], ("OK", "FAILED", "BLOCKED_SEALED_TARGET",
                                           "GATED_NO_ACCEPT", "SKIPPED_DUE_TO_FAILURE"))
            self.assertTrue(node["layer"], f"节点 {node['node']} 没登记层")
        self.assertTrue(rep["generated_at_bjt"].endswith("+08:00"))


class TestTimeDisciplineNew(HistoryTree):
    """新增代码的时间纪律：一律 +08:00、字段后缀 _bjt、无 naive now()。"""

    def test_no_naive_datetime_calls_in_source(self):
        """源码里不许出现 naive 时间调用（本机 PDT，naive 会偏 15 小时且跨日）。"""
        import re as _re
        src = Path(R.__file__).read_text(encoding="utf-8")
        for pat in (r"datetime\.now\(\s*\)", r"utcnow", r"\btime\.strftime\(",
                    r"(import|from)\s+zoneinfo", r"date\.today\(\)"):
            hit = _re.search(pat, src)
            self.assertIsNone(hit, f"源码出现被禁的时间写法 {pat}: "
                                   f"{src[max(0, hit.start() - 60):hit.end() + 40] if hit else ''}")
        self.assertIn("BJT = timezone(timedelta(hours=8))", src)

    def test_new_gate_payload_timestamps_are_bjt(self):
        ctx = self.plant(new_months=self.NEW)
        rep = R.build_layer_report(self.P, ctx, {"status": "DRY_RUN_PLAN", "nodes": []},
                                   [R.ok("G_F", ""), R.ok("G_G", "")])
        self.assertTrue(rep["generated_at_bjt"].endswith("+08:00"))
        self.assertNotIn("+00:00", json.dumps(rep))

    def test_drift_baseline_timestamps_are_bjt(self):
        ctx = self.plant(new_months=self.NEW)
        sig = {"source": "batch", "path": "x", "permille": 250,
               "groups": {"000001.SZ|2026": {"rows": 1, "sig": "f" * 32}}}
        doc = R.write_drift_baseline(self.P, sig, ctx, enabled=True, log=LOG)
        self.assertTrue(doc["recorded_at_bjt"].endswith("+08:00"))


class TestIncrementalAppendEngine(HistoryTree):
    """事实层追加引擎：seed 出来的副本上只 INSERT 新流月的行，历史一字节不动。"""

    def wired_spec(self):
        return ({"role": "main", "table": "monthly_facts", "month_col": "month_id",
                 "wired": True, "source": "batch_data_sqlite", "reason": ""},)

    def make_source_batch(self, months):
        batch = self.docs / "batches" / "MU-APPEND-BATCH"
        db = batch / "data.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        inv_db(db, months)                      # 与主库同构的新流月事实源
        return batch

    def test_engine_appends_only_new_months_then_gate_f_passes(self):
        ctx = self.plant(new_months=())          # staging = 历史副本，还没加新月份
        batch = self.make_source_batch(self.NEW)
        ctx.batch_dir = batch
        ctx.new_months = R.derive_new_months(self.P, ctx, LOG)
        self.assertEqual(ctx.new_months, tuple(self.NEW))
        orig = R.FACT_APPEND_SPECS
        try:
            R.FACT_APPEND_SPECS = self.wired_spec()
            rec = R.apply_incremental_facts(self.P, ctx, enabled=True, log=LOG)
        finally:
            R.FACT_APPEND_SPECS = orig
        self.assertEqual(len(rec["applied"]), 1)
        self.assertEqual(rec["pending_script"], [])
        before = sqlite3.connect(str(self.P.main_db)).execute(
            "SELECT COUNT(*) FROM monthly_facts").fetchone()[0]
        con = sqlite3.connect(str(self.st_main))
        n_new = con.execute("SELECT COUNT(*) FROM monthly_facts WHERE month_id=?",
                            (self.NEW[0],)).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM monthly_facts").fetchone()[0]
        old_overlap = con.execute("SELECT COUNT(*) FROM monthly_facts WHERE month_id!=?",
                                  (self.NEW[0],)).fetchone()[0]
        con.close()
        self.assertEqual(n_new, len(INV_IDENTITIES), "新流月应按身份各插一行")
        self.assertEqual(total, before + n_new)
        self.assertEqual(old_overlap, before, "追加不该动历史行")
        ctx.new_months = R.derive_new_months(self.P, ctx, LOG)
        self.assertEqual(ctx.new_months, tuple(self.NEW))
        g = self.gate_f(ctx)
        self.assertEqual(g.verdict, "PASS", json.dumps(g.detail, ensure_ascii=False)[:1500])

    def test_engine_refuses_when_source_unreadable(self):
        ctx = self.plant(new_months=())
        ctx.batch_dir = self.docs / "batches" / "MU-NO-SUCH-BATCH"
        orig = R.FACT_APPEND_SPECS
        try:
            R.FACT_APPEND_SPECS = self.wired_spec()
            with self.assertRaises(RuntimeError) as cm:
                R.apply_incremental_facts(self.P, ctx, enabled=True, log=LOG)
            self.assertIn(R.E_APPEND_SCOPE_EMPTY, str(cm.exception))
        finally:
            R.FACT_APPEND_SPECS = orig

    def test_registry_is_honest_about_unwired_steps(self):
        """没接线的步骤必须登记成 pending，而不是被当成已经增量完成。"""
        ctx = self.plant(new_months=self.NEW)
        rec = R.apply_incremental_facts(self.P, ctx, enabled=True, log=LOG)
        self.assertEqual(rec["applied"], [], "生产登记表里目前不该有假装能跑的 wired 项")
        self.assertGreaterEqual(len(rec["pending_script"]), 4)
        self.assertTrue(all(x["reason"] for x in rec["pending_script"]))
        self.assertTrue(len(R.INTEGRATION_TODOS) >= 4)

    def test_seed_staging_is_a_real_copy_of_live(self):
        P = self.P
        ctx = self.plant(new_months=())
        st = P.local_db_dir / f"staging_{ctx.run_id}.sqlite"
        st.unlink()
        rec = R.seed_staging_from_live(P, ctx, enabled=True, log=LOG, roles=("main",))
        self.assertEqual(len(rec["seeded"]), 1)
        con = sqlite3.connect(str(st))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM monthly_facts").fetchone()[0],
                         sqlite3.connect(str(P.main_db)).execute(
                             "SELECT COUNT(*) FROM monthly_facts").fetchone()[0])
        con.close()
        self.assertEqual(list(P.local_db_dir.glob("*.part.*")), [], "seed 留了半截文件")


class TestDriftBlocksWholeChain(FakeTree):
    """端到端反向断言：上游修订过历史 ⇒ 整链拒绝增量追加（不 --accept、不动正式库、不改基线）。"""

    def setUp(self):
        self.P = self.fresh_state()
        # 现网库换成「有历史流月」的真表（否则 old_months=∅ ⇒ 所有月都算新增加，
        # G-G 的历史区间就空了，测不到东西）
        inv_db(self.P.main_db, ("2024-M01", "2026-M08"))
        inv_db(self.P.etf_db, ("2024-M01", "2026-M08"))
        # 假扫描器：产出按票×年的历史行；FAKE_DRIFT 模拟「上游把 600018.SH 的 2026 历史改了」
        self.py_script(self.scanner, """
DOCS = pathlib.Path(os.environ['FAKE_DOCS'])
seq = int(os.environ.get('FAKE_SEQ', '0'))
drift = float(os.environ.get('FAKE_DRIFT', '0'))
new = DOCS / 'batches' / ('MUFAKE-%d' % seq)
(new / 'reports').mkdir(parents=True, exist_ok=True)
db = new / 'data.sqlite'
if db.exists(): db.unlink()
con = sqlite3.connect(str(db))
con.execute('CREATE TABLE calendar_months(month_id TEXT, term TEXT, start_at TEXT)')
con.execute('CREATE TABLE monthly_facts(stock_id TEXT, month_id TEXT, solar_year INTEGER,'
            ' close REAL, direction TEXT)')
for m in ('2024-M01', '2026-M08', '2026-M11'):
    for sid in ('000001.SZ', '600018.SH'):
        close = 10.0 + len(m) + (drift if (sid == '600018.SH' and m.startswith('2026')) else 0.0)
        con.execute('INSERT INTO monthly_facts VALUES(?,?,?,?,?)',
                    (sid, m, int(m[:4]), close, 'UP'))
con.commit(); con.close()
(new / 'reports/golden_report.json').write_text('{"status":"PASS"}', encoding='utf-8')
""")

    def argv(self, run_id, *more):
        return ["--run-id", run_id, "--trigger", "auto", "--execute", "--as-of", "2026-11-09",
                "--accept-gates", *more]

    def test_chain_refuses_and_keeps_live_dbs_untouched(self):
        P = self.P
        self.write_baseline(P)
        extra = {R.RELATION_REBUILD_ENV: "authorized"}
        rc1, out1 = self.cli(self.argv("MU-20261120T140000", "--force-drift-baseline"), extra)
        st1 = json.loads(P.state_file.read_text(encoding="utf-8"))
        self.assertEqual(st1["status"], "SUCCESS",
                         json.dumps(st1["gates"], ensure_ascii=False)[:2500])
        self.assertTrue(P.drift_baseline.exists(), "首轮人工建档后应留下 G-G 基线")
        base = json.loads(P.drift_baseline.read_text(encoding="utf-8"))
        self.assertEqual(base["run_id"], "MU-20261120T140000")
        self.assertGreaterEqual(base["group_count"], 2)
        main_after_run1, etf_after_run1 = sha(P.main_db), sha(P.etf_db)

        # 下一轮：上游对 600018.SH 的 2026 历史做了修订 ⇒ 同键校验和不再等值
        rc2, out2 = self.cli(self.argv("MU-20261121T140100"), dict(extra, FAKE_DRIFT="1000"))
        st2 = json.loads(P.state_file.read_text(encoding="utf-8"))
        gg = st2["gates"]["G_G"]
        self.assertEqual((gg["verdict"], gg["code"]), ("FAIL", R.E_HISTORY_DRIFT),
                         json.dumps(gg, ensure_ascii=False)[:1500])
        self.assertIn("600018.SH", json.dumps(gg["detail"], ensure_ascii=False))
        self.assertEqual(st2["status"], "STAGED_ONLY", json.dumps(st2, ensure_ascii=False)[:800])
        self.assertEqual(st2["error_code"], R.E_HISTORY_DRIFT)
        self.assertNotIn("--accept", self.argv_of_extensions(), "历史漂移却传了 --accept")
        self.assertEqual(sha(P.main_db), main_after_run1, "G-G 失败仍动了正式主库")
        self.assertEqual(sha(P.etf_db), etf_after_run1, "G-G 失败仍动了 ETF 正式库")
        doc = json.loads(P.drift_baseline.read_text(encoding="utf-8"))
        self.assertEqual(doc["run_id"], "MU-20261120T140000", "门禁没过却更新了基线")
        self.assertEqual(list(P.work.glob("*.tmp.*")), [], "原子写留了临时文件")
        self.assertEqual(rc2, 1)

    def test_full_mode_is_not_blocked_by_the_same_drift(self):
        """同一处历史修订在 --mode full 下只登记不拦（季度互验就是要重算）。"""
        P = self.P
        self.write_baseline(P)
        extra = {R.RELATION_REBUILD_ENV: "authorized"}
        self.cli(self.argv("MU-20261120T140200", "--force-drift-baseline"), extra)
        rc, out = self.cli(["--run-id", "MU-20261122T140300", "--trigger", "auto", "--execute",
                            "--as-of", "2026-11-09", "--accept-gates", "--mode", "full"],
                           dict(extra, FAKE_DRIFT="1000"))
        st = json.loads(P.state_file.read_text(encoding="utf-8"))
        gg = st["gates"]["G_G"]
        self.assertEqual(gg["verdict"], "PASS", json.dumps(gg, ensure_ascii=False)[:1200])
        self.assertGreater(gg["detail"]["drifted_count"], 0)
        self.assertEqual(st["update_mode"], "full")
