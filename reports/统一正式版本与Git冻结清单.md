# 统一正式版本与 Git 冻结清单

- 生成时间：2026-10-20 02:48 UTC（2026-10-19 19:48 PDT）
- 状态：**已冻结**
- 冻结依据：P6 正式发布成功 + 发布后 29 项 API 兼容 0 差异 + 22 项功能回归全通过 + 两库哈希未变

## 1. 版本标识

| 项 | 值 |
|---|---|
| 仓库 | `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1` |
| 分支 | `main` |
| HEAD | `bf66e03429552ee401de814874495ac6238613cf` |
| **正式发布标签** | `release/tg-local-v1.0.0-p6` → `eb9cffdcf822d13a2222cc8119193f2bd138c080`（annotated，指向 `bf66e03`） |
| 回滚基线标签 | `baseline/pre-ten-god-v1` → `fe6ea01089a69fa82bb01188aee2a4f7e5c5ae84` |
| 候选标签（历史保留） | `release/tg-local-v1.0.0` → `230fe67f544e61e820f1a0caab8f95df5f204b6e` |
| 规则版本 | 十神 `tg-local-v1.0.0`；藏干 `tg-hidden-v1.0.0` |
| 旧关系规则版本（沿用） | `bazi-relation-research-v1.3.0-provisional` |

标签说明：`release/tg-local-v1.0.0` 是发布**候选**标签（位于文档提交），按其语义保留不动；正式发布另建 `-p6` 标签并指向实际部署的代码提交 `bf66e03`。两者 `app/` 内容经 `git diff --stat` 验证字节级相同。

## 2. 运行时代码指纹（部署内容）

| 文件 | SHA-256 |
|---|---|
| `app/api.py` | `a72273218f66fde742439db29ca9749134638ef9da3977115237bbd1934e3bf2` |
| `app/server.py` | `c3c872fdd5b05a03351a085bd61ea00e136d44e3a87bd0c0ac09e44b013a50f1` |
| `app/config.py` | `dc967c3ae687faee32ad19762f4d1cce6c3cedac31f2db7efd19c2055e4f5edb` |
| `app/ten_god_rules.py` | `754897909cbc37d5a76015cc6767faa60e76fdcdf59faacf49f869fb0bf6976f` |
| `app/ten_god_service.py` | `88378847a241095f0e9e72cbb1e9ce9bff4ef1d6e14aa86e15e0457afbfac640` |
| `app/static/index.html` | `b3c2da283c026bd6fe1d19dc415aa65dd7de379488f543c8b9761e26426d55a9` |
| `app/static/app.js` | `1174c89d25143b302f41814585928da265639a1d58101df02fa219108d0415e5` |
| `app/static/style.css` | `81c13be79fb075270ec5574ad20497dd67c34dafb0753cd33202c72b8fac765b` |
| `app/rules/ten_god_reference_v1.json` | 运行时读取（启动时 100 格校验夹具，0 不一致才放行）；由 `tools/build_rule_files.py` 从 `v13_shadow_ready_20261018/release_candidate/rules/V121_verified_reference.json` 生成 |
| `app/rules/ten_god_hidden_stems_v1.json` | 运行时读取（模块导入时加载，进程启动必需）；12 支藏干源顺序，无权重；来源 `stock-metaphysics-platform/src/core/constants.py::BRANCH_HIDDEN_STEMS` |

## 3. 运行时事实

| 项 | 值 |
|---|---|
| 正式端口 | `127.0.0.1:8737`（仅本机） |
| PID | 32360 |
| 工作目录 | `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app` |
| Python | `/opt/homebrew/Cellar/python@3.14/3.14.7/…/Python`（3.14.7） |
| 启动日志 | `evidence/prod_8737.log` |
| 启动器（唯一） | `bazi_month_relation_tengod_dev_v1/启动十神版.command`（默认 8737，`BAZI_PORT` 可覆盖） |
| 兼容入口（转发） | `bazi_month_relation_local_v1/启动八字历史规律查询.command` → 上一条启动器 |
| 数据源 | A股 `…/bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite`（只读）<br>ETF `…/bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite`（只读） |

## 4. 数据库冻结（未变更）

| 库 | 大小 | mtime | SHA-256 |
|---|---:|---:|---|
| A 股 | 5,129,752,576 | 1792339724 | `cc2d4dc43448c9c248d318627d4c5ccb37575645041772cf87aca73dea60ab46` |
| ETF | 446,578,688 | 1792411734 | `e67e13d4b620d99f70ce89b1096f407577ab5a87cbb42d661a3e960172f3db18` |

发布前后三项指标全部一致；无 WAL/SHM；本项目全程以 `mode=ro` + `PRAGMA query_only=ON` 访问。

## 5. 归档资产（保留，不删除）

| 资产 | 路径 | 用途 |
|---|---|---|
| 旧版 app 快照（目录） | `bazi_month_relation_local_v1/app/`（未改动） | 旧代码仍在原位，可用于回滚启动 |
| P6 备份 | `_p6_backup_20261019_194247/`（808 KB，含 SHA256SUMS、`.command.bak`） | 取证；不可从其启动（相对库路径失效） |
| 旧版 Git 基线 | 标签 `baseline/pre-ten-god-v1` | 代码考古/差异对比 |
| 升级前 API 快照 | `evidence/api_baseline_8737_pre_p6.json`（`826145a0…`） | 旧行为不可变基准 |
| 切换前即时快照 | `evidence/api_baseline_8737_cutover.json`（`1cef45f0…`） | 证明切换前无漂移 |
| 发布后兼容报告 | `evidence/api_compat_post_release.json` | 29/0 |
| 回归与 UI 证据 | `evidence/p6_prod_regression.json`、`screenshots_prod_8737/` | 22/22、13 截图 |
| 文档 | `reports/`（P0–P7 共 15 份） | 交付与追溯 |

## 6. 冻结后的开发纪律

1. **唯一开发基线**：此后所有新功能只从 `release/tg-local-v1.0.0-p6`（= `bf66e03`）创建分支，分支命名建议 `feat/<主题>`。
2. **禁止**：
   - 不得再分别修改两套旧代码（`bazi_month_relation_local_v1/app/` 与十神版以外的旧副本）；
   - 不得直接提交到 `main` 之外又在 8737 上运行未评审代码；
   - 不得覆盖、移动、删除既有标签；
   - 不得对两套 SQLite 执行写操作、`VACUUM`、`CREATE INDEX`、`ALTER TABLE`；
   - 不得复制数据库进开发副本（`.gitignore` 已排除 `*.sqlite`/`*.db`/`*.parquet`/`*.csv.gz`/`*.zip`）。
3. **临时测试端口**：允许用 `BAZI_PORT=88xx ./启动十神版.command` 临时验证，用完即停；不得常驻第二套正式服务。
4. **再发布门槛**：任何后续发布前必须重跑 32 项单测 + 快照兼容（以新快照为基准）+ 功能回归，并重新记录指纹。

## 7. 冻结确认

| 项 | 值 |
|---|---|
| Git 工作区 | 本报告定稿后提交，随后 `git status --porcelain` 应为空 |
| 正式服务 | 8737 十神版，PID 32360 |
| 测试 | 32/32 单测；29/29 快照兼容；22/22 功能回归 |
| 数据 | 两库零变更 |
