# 正式上线前后 API 对照方案

- 适用任务：`BAZI-TEN-GOD-P6-PREFLIGHT`
- 目的：解决“升级后不能再用同一个 8737 同时充当 OLD 和 NEW”的问题
- 结论：已改为**升级前快照 + 升级后逐字段比对**，并已用候选版与旧版回滚实例各自验证 0 差异。

## 1. 原方案的问题

原 `tools/api_compat_check.py` 同时请求 8737（OLD）与 8848（NEW）。一旦 8737 被升级：

- 两个地址都是新版，比较结果必然 0 差异，**无法证明旧字段未变**；
- 若升级后才发现差异，已没有可信的旧响应可对照；
- 也无法区分“旧字段变化”与“新增字段”。

## 2. 新方案：不可变快照

### 2.1 快照文件

| 项目 | 值 |
|---|---|
| 文件 | `evidence/api_baseline_8737_pre_p6.json` |
| 抓取时间（UTC） | 2026-10-20T02:00:27+00:00 |
| 抓取时间（CST） | 2026-10-20T10:00:27+08:00 |
| 来源 | `http://127.0.0.1:8737`（升级前旧版） |
| 请求数 | **29** |
| 文件 SHA-256 | `826145a07e48bbaa78c09464c6235218927ce624e9fabd52dd327e04a8f89566` |
| 旧代码指纹 | `api.py e5c49827…`、`server.py eefa9df8…`、`index.html 03f568ba…`、`app.js 94f320bc…`、`style.css 81c13be7…` |
| 旧库指纹 | A 股 5,129,752,576 B / mtime 1792339724；ETF 446,578,688 B / mtime 1792411734 |

快照同时保存每条响应的 `http_status`、`bytes`、`path`、`params`、`body`，可直接离线审计。

### 2.2 覆盖面（29 项）

| 类别 | 请求 |
|---|---|
| 档案 | `/api/stock` × 3（510050 ETF、000001 A 股、000003 缺档案） |
| 12 支 | `/api/branch12` × 5（ETF/A 股、ALL/EXCLUDE_SINGLE） |
| 12 支明细 | `/api/branch_detail` × 2 |
| 60 柱 | `/api/pillar60` × 3（ETF、A 股、缺档案） |
| 60 柱明细 | `/api/pillar_detail` × 2（己亥、壬寅） |
| 关系 | `/api/relation_types` × 2、`relation_pillars` × 1、`relation_months` × 1 |
| 月详情 | `/api/month_detail` × 2（含单交易日 2002-M01） |
| 历法 | `/api/calendar_now` × 2（ETF/A 股） |
| 元信息 | `/api/meta` × 2、`catalog`、`sources`、`pillar_options`、`search` |

覆盖旧版全部只读查询入口，并额外覆盖单交易日两种口径。

## 3. 升级前：抓取快照

```bash
cd "/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1"
/Users/zhangxingjin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 tools/api_snapshot.py \
  --base http://127.0.0.1:8737 \
  --out evidence/api_baseline_8737_pre_p6.json \
  --label pre-p6-8737
```

验收：

- 29 条全部 `200`；
- 文件 SHA-256 记入发布记录；
- 若 8737 代码指纹或数据库 mtime 与上表不一致，必须重新抓取并生成新快照，**不得复用旧快照**。

## 4. 升级后：与快照比对

```bash
cd "/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1"
/Users/zhangxingjin/.workbuddy/binaries/python/versions/3.13.12/bin/python3 tools/api_compat_check.py \
  --baseline evidence/api_baseline_8737_pre_p6.json \
  --target http://127.0.0.1:8737 \
  --out evidence/api_compat_post_release.json
```

验收：

```
ENDPOINTS=29 TOTAL_DIFFS=0
BASELINE_SHA256=826145a07e48bbaa78c09464c6235218927ce624e9fabd52dd327e04a8f89566
```

- 退出码 `0` 才算通过；非 0 即 `BLOCKED_RELEASE`。
- 任一旧字段缺失、改值、列表长度变化，均不得用“页面看起来正常”替代。

## 5. 比较规则

### 5.1 判定方式

对每条响应做**旧字段子集比较**：

- 旧 JSON 的每个键必须在新 JSON 中存在；
- 每个旧值必须完全相等（字符串、数字、布尔、`null` 均严格比较）；
- 列表长度必须一致，逐项比较；
- 新 JSON 多出的键允许存在（十神增量字段）。

### 5.2 允许新增的字段

`ten_god`、`ten_god_status`、`flow_hidden_stem_ten_gods`、`hidden_stem_summary`、`stem_ten_god_computed`、`stem_ten_god_consistent`、`fact_status`、`ten_god_rule_version`、`ten_god_hidden_rule_version`、`day_master`、`day_master_status`、`no_price_n`、`index_comparison_reason` 等。

### 5.3 不允许的变化

- 旧字段被删除或改名；
- `body_return` / `benchmark_return` / `excess_return` / `direction` / `single_day` / `n` / `up_n` 等数值变化；
- 列表条数变化（例如 60 柱变 59 行、12 支变 11 行）；
- 状态码非 200；
- 数据集串库（ETF 返回 A 股身份）。

## 6. 差异处理

| 差异类型 | 判定 | 动作 |
|---|---|---|
| 旧字段缺失 | BLOCKED | 停止发布，回滚，保留 `api_compat_post_release.json` |
| 旧值变化 | BLOCKED | 同上；先定位是代码、数据还是口径变化 |
| 列表长度变化 | BLOCKED | 同上；优先检查 SQL 过滤条件 |
| 仅新增字段 | PASS | 记录新增字段清单 |
| 请求失败/超时 | BLOCKED | 检查服务是否存活、端口是否正确 |

任何 BLOCKED 都不得通过修改快照“修到通过”。

## 7. 已完成的验证

| 目标 | 版本 | 结果 | 证据 |
|---|---|---|---|
| `http://127.0.0.1:8848` | 十神候选版 | **29/29，0 差异** | `evidence/api_compat_report.json`、`p6_api_compat_8848.log` |
| `http://127.0.0.1:8851` | 旧版（从原目录启动，回滚演练） | **29/29，0 差异** | `evidence/api_compat_rollback_8851.json`、`p6_api_compat_rollback.log` |

两项均使用同一快照 SHA `826145a0…`，证明：
- 候选版没有改变任何旧字段；
- 回滚路径能恢复到与升级前完全一致的 API 行为。

## 8. 快照有效期与刷新

- 快照只对“抓取时的 8737 代码 + 两库状态”有效。
- 发布前若发生以下任一情况，必须重新抓取：
  - 8737 代码文件哈希变化；
  - 任一数据库 mtime 或大小变化；
  - 上游冻结批次被重建；
  - 距抓取超过 24 小时且期间有任何写入。
- 新快照必须记录新 SHA，并在 `P6正式发布前最终验收报告.md` 中更新。

## 9. 限制

- 只覆盖 GET 查询接口；本项目没有写接口。
- 只比较响应体，不比较 HTTP 头、耗时；性能单独由 `evidence/perf.json` 记录。
- 快照包含真实证券数据，但均为本地只读查询结果，不含凭证。
- 快照文件必须与发布记录一同归档，不得只留在临时目录。
