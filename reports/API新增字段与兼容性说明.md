# API 新增字段与兼容性说明

- 开发副本：`bazi_month_relation_tengod_dev_v1`
- 预览：`http://127.0.0.1:8848/`
- 规则版本：`tg-local-v1.0.0` / 藏干 `tg-hidden-v1.0.0`
- 原则：**只增加字段，不删除、不改名、不改旧值**。

## 1. 接口增量一览

| 接口 | 新增字段 | 说明 |
|---|---|---|
| `/api/stock` | `ten_god_rule_version`、`ten_god_hidden_rule_version`、`day_master_status`、`day_master` | 日主状态 `OK` / `UNAVAILABLE_PROFILE` |
| `/api/pillar60` | 每行 `stem_ten_god`、`ten_god_status`、`ten_god`（完整对象）、`flow_hidden_stem_ten_gods`、`hidden_stem_summary`、`day_master_stem`、`day_master_status` | 60 柱主表；不改原 `occurrence_count` / `up_n_ALL` 等 |
| `/api/pillar_detail` | 每行 `ten_god`、`ten_god_status`、`flow_hidden_stem_ten_gods`、`hidden_stem_summary`、`stem_ten_god_computed`、`stem_ten_god_consistent` | 保留原 `stem_ten_god`，新增复算值与一致性布尔 |
| `/api/branch_detail` | 每行 `ten_god`、`ten_god_status`、`flow_hidden_stem_ten_gods`、`hidden_stem_summary` | 12 支明细逐月展示 |
| `/api/relation_months` | 每行 `ten_god`、`ten_god_status`、`flow_hidden_stem_ten_gods`、`hidden_stem_summary` | 不改关系类型、柱位、事件数 |
| `/api/month_detail` | `ten_god`、`ten_god_status`、`flow_hidden_stem_ten_gods`、`hidden_stem_summary` | 月详情弹窗 |
| `/api/calendar_now` | `identity_code`、`day_master`、`ten_god_status`、`ten_god_rule_version`；`current.ten_god`、`current.fact_status`；`offset=1` 的 `ten_god`、`fact_status` | 只给当前与下一流月；**不返回任何未来行情字段** |
| `/api/meta` | `ten_god_rule_version`、`ten_god_hidden_rule_version`、`ten_god_rule_source` | 来源与版本页展示 |
| **新增** `/api/ten_god_stats` | 10 类聚合 | 见第 3 节 |
| **新增** `/api/ten_god_months` | 某一十神的逐月明细 | 复用冻结 `monthly_facts`，不重算月线 |

> 所有历史单月行（`pillar_detail` / `branch_detail` / `relation_months` / `month_detail` / `ten_god_months`）的 `ten_god` 对象另含 `fact_status`；60 柱聚合行没有单月方向，`fact_status=null`。

## 2. 状态语义

| 状态 | 触发条件 | 行为 |
|---|---|---|
| `OK` | 日主合法 + 流月柱合法 | 返回十神与藏干 |
| `UNAVAILABLE_PROFILE` | 无档案 / `profile_present=0` / 日主非法 | `stem=null`、`hidden_stems=[]`，不猜 |
| `INVALID_PILLAR` | 非两字、非法干支、阴阳不配 | 同上，附原因 |

**事实层状态**（`ten_god.fact_status` / 月条目 `fact_status`，与十神状态分开）：

| 状态 | 含义 |
|---|---|
| `HISTORICAL_FACT` | 该月有已冻结的月事实，且有有效方向 |
| `NO_HISTORICAL_FACT` | 未来月或当前未收盘月：没有 `monthly_facts` 行；干支/十神仍可展示，但没有收益 |
| `NO_VALID_PRICE_SAMPLE` | 有月事实但无有效行情（`direction IS NULL`）；当前两库 0 行 |

统计接口每组另有 `fact_status`、`no_price_n`：整组都无有效行情时为 `NO_VALID_PRICE_SAMPLE`，否则 `HISTORICAL_FACT`；零样本为 `NO_HISTORICAL_FACT`。

## 3. `/api/ten_god_stats` 合同

参数：`id`、`dataset`（`ashare`/`etf`）、`sample_mode`（`ALL`/`EXCLUDE_SINGLE`）、可选 `flow_branch`、`relation_type`。

返回（510050.SH / ETF / ALL 的真实节选）：

```json
{
  "status": "OK",
  "rule_version": "tg-local-v1.0.0",
  "hidden_rule_version": "tg-hidden-v1.0.0",
  "identity_code": "510050.SH",
  "day_master": "戊",
  "sample_mode": "ALL",
  "basis": "EXISTING_VALID_MONTHLY_FACTS",
  "filters": {"flow_branch": null, "relation_type": null},
  "groups": [
    {"ten_god": "比肩", "n": 26, "up": 14, "down": 12, "flat": 0,
     "up_rate": 0.538462, "mean_return": 0.0029, "return_n": 26,
     "single_day_n": 0, "no_price_n": 0, "fact_status": "HISTORICAL_FACT",
     "benchmark_n": 26,
     "index_comparison": {"benchmark_n": 26, "benchmark_mean_return": 0.0021,
                          "excess_mean": 0.0008, "outperform_n": 13},
     "index_comparison_reason": null,
     "sample_grade": "RELATIVELY_MORE", "stability_note": ""},
    {"ten_god": "劫财", "n": 26, "up": 15, "down": 11, "flat": 0,
     "up_rate": 0.576923, "mean_return": 0.0142, "return_n": 26,
     "single_day_n": 0, "no_price_n": 0, "fact_status": "HISTORICAL_FACT",
     "benchmark_n": 26,
     "index_comparison": {"benchmark_n": 26, "benchmark_mean_return": 0.0069,
                          "excess_mean": 0.0072, "outperform_n": 15},
     "index_comparison_reason": null,
     "sample_grade": "RELATIVELY_MORE", "stability_note": ""}
  ],
  "unavailable": {"missing_profile": 0, "invalid_price": 0},
  "accounting": {"total_rows": 260, "classified_n": 260,
                 "missing_profile": 0, "unclassified": 0},
  "denominator_note": "n = 同筛选下的有效历史月事实数……",
  "prediction": false
}
```

说明：上例只节选前两组，完整 10 组见 `evidence/api_samples.json`。数值均为真实库输出，不是占位。

## 4. `/api/ten_god_months` 合同

参数：`id`、`dataset`、`ten_god`、`sample_mode`、可选 `flow_branch`、`relation_type`。

返回 `{status, ten_god, n, rows:[...], rule_version, prediction:false}`。每行取自 `monthly_facts`，含 `month_id / flow_pillar / start_term / first_date / last_date / trading_days / direction / body_return / benchmark_return / excess_return / comparison_status / official_types / hidden_stem_summary`。

## 5. 向后兼容核验（硬证据）

工具：`tools/api_snapshot.py`（升级前抓取）+ `tools/api_compat_check.py`（升级后比对）。

**方法（P6 修正）**：不再把同一个 8737 同时当作 OLD 和 NEW。

1. 升级前（8737 仍是旧版）抓取原始响应，落盘为快照：
   `evidence/api_baseline_8737_pre_p6.json`，SHA-256 `826145a07e48bbaa78c09464c6235218927ce624e9fabd52dd327e04a8f89566`。
2. 升级后把目标服务（候选端口或新版 8737）与快照逐字段比较：
   旧键必须存在且值完全相同；允许新键；列表长度必须一致。
3. 快照含 29 个请求，覆盖 A 股/ETF、ALL/EXCLUDE_SINGLE、60 柱、12 支、关系、月详情、历法、meta 等。

结果：**29 个请求 / 0 处差异**（对候选 8848）。

```
ENDPOINTS=29 TOTAL_DIFFS=0
BASELINE_SHA256=826145a07e48bbaa78c09464c6235218927ce624e9fabd52dd327e04a8f89566
```

原始报告：`evidence/api_compat_report.json`；对照日志 `evidence/p6_api_compat_8848.log`。

> 旧版 19 项核验仍成立，本轮扩展到 29 项（新增单交易日两种口径、A 股关系与月详情、历法、搜索等）。

## 6. 旧值一致性

- `pillar_detail` 的 510050 己亥四行：`stem_ten_god` 均为 `劫财`，`stem_ten_god_computed` 也为 `劫财`，`stem_ten_god_consistent=true`。
- 两库全部非空行复算：A 股 848,995 行、ETF 75,352 行，**0 不一致**（`evidence/p5_data_integrity.log`）。
- `month_detail` 的旧字段（`body_return`、`benchmark_return`、`excess_return`、`row_sha256` 等）原样返回。

## 7. 已知边界

- `/api/calendar_now` 只对当前与下一流月补十神；更远月份仍只有干支与日期。
- `branch12`、`relation_types`、`relation_pillar` 等**汇总层**不增加“唯一十神”，因为同一地支/关系对应不同流月干，语义不成立。
- 十神计算全部在读取时完成，不写库、不建缓存表。
