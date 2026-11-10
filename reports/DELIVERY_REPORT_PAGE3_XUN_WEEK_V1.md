# 页面三「60流月柱历史规律」月/旬/周三层窗口视图交付验收报告

## 1. 一句结论
在工作分支 `feat/page3-xun-week-windows` 上，已成功为本地查询前台「页面三·60流月柱历史规律」新增“月 / 旬 / 周”三层窗口池层汇总视图，并联动个股抽屉「月内窗位」3旬/6周收益小表与5年循环细查；全库行数、空窗锚点、计数闭合、样本回查与UI交互全量验收通过，未动生产 8737 端口与原始数据。

---

## 2. 改动文件清单（绝对路径）

| 文件绝对路径 | 类型 | 说明 |
|---|---|---|
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/config.py` | 修改 | 配置 `XUN_WEEK_DB` 路径并加入启动自检 |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/api.py` | 修改 | 新增 `connect_xun_week()` 只读挂载、池层汇总 `window_layer_pool()`、个股窗位规则 `stock_window_rules()` |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/server.py` | 修改 | 路由新增 `/api/window_layer_pool` 与 `/api/stock_window_rules` |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/static/index.html` | 修改 | 页面三顶部新增 `#p-layer-bar`（月/旬/周分段切按钮）及口径红线提示条；个股抽屉新增 `#drawer-windows-box` |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/static/style.css` | 修改 | 新增柱子分组样式 `.grp-even`、`.grp-odd`、`.grp-border-top` 与窗位展示卡片样式 |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/static/app.js` | 修改 | 改造 `renderPillar()` 支持分层聚合查询、行分组底色、切层保留筛选；抽屉接入 `renderDrawerWindowSection()` |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/tests/test_xun_week_acceptance.py` | 新增 | 自动化验收测试脚本（行数核对、空窗锚点、G06计数闭合、数字回查、反向断言） |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/tools/xun_week_walk.js` | 新增 | Playwright/Puppeteer 端到端浏览器自动化走通与截图脚本 |
| `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/reports/DELIVERY_REPORT_PAGE3_XUN_WEEK_V1.md` | 新增 | 本交付报告 |

---

## 3. 前台唯一读取路径清单（只读，严禁写入）

1. **旬/周规则与逐次事实库**：
   - 路径：`/Volumes/workSSD/software development/AStock_History_Scan/allstock_xun_week_v1/database/allstock_xun_week_v1.sqlite`
   - 打开方式：`sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)` + `PRAGMA query_only=ON`
2. **正式 A 股基础库**：
   - 路径：`/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_local_v1/database/bazi_month_relation_local_v1.sqlite`
   - 打开方式：`mode=ro` + `PRAGMA query_only=ON`
3. **ETF 基础库**：
   - 路径：`/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_etf_v1/database/bazi_month_relation_etf_v1.sqlite`
   - 打开方式：`mode=ro` + `PRAGMA query_only=ON`
4. **市值分档库（扩展，可选）**：
   - 路径：`/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_local_v1/extensions/market_cap_tier_v1/database/market_cap_tier_v1.sqlite`
   - 打开方式：`ATTACH DATABASE ... mode=ro`

---

## 4. 新增/修改的接口与列

### 4.1 后端 API 接口
1. `GET /api/window_layer_pool`
   - **入参**：
     - `layer`: `month`（月层 60 柱）/ `xun`（旬层 180 窗）/ `week`（周层 360 窗）
     - 筛选条件：`pillar`, `flow_stem`, `flow_branch`, `stem_ten_god`, `branch_ten_god`, `up_rate_min`, `up_rate_max`, `min_n`, `sort`, `dir`
   - **出参**：
     - `total`: 记录数
     - `layer`: 当前层级
     - `rows`: 聚合后的池层记录列表
     - 服务端已接入 `_POOL_CACHE` 内存缓存，全市场聚合响应延迟 $< 5\text{ms}$。
2. `GET /api/stock_window_rules`
   - **入参**：`identity`（股票识别码），`pillar`（流月柱，可选）
   - **出参**：
     - `xun_rules`: 该票在该柱下的 3 个旬规则
     - `week_rules`: 该票在该柱下的 6 个节气周规则
     - `occurrences`: 该票按各 5 年循环年展开的 3 旬 / 6 周原始交易日收益明细矩阵

### 4.2 页面三池层视图列清单与红线明示
- `流月柱`: 干支柱名（如 甲子、丙午）
- `窗位`:
  - 旬层：上旬 (0–10日) / 中旬 (10–20日) / 下旬 (20–下节)
  - 周层：第1周 (0–5日) / 第2周 (5–10日) / 第3周 (10–15日) / 第4周 (15–20日) / 第5周 (20–25日) / 第6周 (25–下节)
- `覆盖股票数`: 池层加权覆盖的纯 A 股数量（如 5,329 只），严禁显示虚假常数
- `有效窗 / 应有窗`: 显示 `ALL_valid_n / ALL_n_occurrences`（如 31,489 / 31,489）
- `涨 / 跌 / 平`: 统计数字严格满足 $up + down + flat = valid$
- `历史上涨占比`: 上涨占比百分比及进度条
- `平均涨跌幅`: 均值百分比
- `中位数涨幅`: 中位数百分比
- `最大跌幅` / `最大涨幅`: 极值百分比
- `天干五行` / `地支五行`: 元素标识
- **口径红线在表头顶部明确标注**：
  > 窗收益 = 该窗内首根有效日K开盘 → 末根有效日K收盘（非月涨幅拆分）；空窗（无交易日）不计入分母；平盘严格要求涨跌幅 = 0。

### 4.3 个股侧边抽屉新增「月内窗位」小表
- 提供「旬层（3旬）」与「周层（6周）」单选切换按钮；
- 窗位卡片：显示窗位名称、区间天数、涨/跌/无交易日状态、有效样本覆盖率、该窗收益；
- 5 年循环逐次原始证据矩阵：逐年穿透，展示历次该节气月该窗位真实涨跌幅（空窗显示为“无交易日”，标灰色标签）。

---

## 5. 实测验收数字（真跑实测对账表）

运行命令：
```bash
$PY /Volumes/workSSD/software\ development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/tests/test_xun_week_acceptance.py
```

### 5.1 【验收 1】行数核对
| 数据表 | 预期行数 | 实测行数 | 判定 |
|---|---|---|---|
| `month_rules` | 316,605 | 316,605 | PASS |
| `xun_rules` | 949,815 | 949,815 | PASS |
| `week_rules` | 1,899,630 | 1,899,630 | PASS |
| `month_occurrences` | 908,298 | 908,298 | PASS |
| `xun_occurrences` | 2,724,894 | 2,724,894 | PASS |
| `week_occurrences` | 5,449,788 | 5,449,788 | PASS |

### 5.2 【验收 2】空窗锚点核对
| 窗口层级 | 预期空窗数 `SUM(ALL_invalid_n)` | 实测统计数 | 判定 |
|---|---|---|---|
| 旬层 (`xun_rules`) | 42,847 | 42,847 | PASS |
| 周层 (`week_rules`) | 217,224 | 217,224 | PASS |

### 5.3 【验收 3】G06 计数闭合断言
断言规则：全量每行必须严格满足 `ALL_valid_n + ALL_invalid_n == ALL_n_occurrences` 且 `ALL_up_n + ALL_down_n + ALL_flat_n == ALL_valid_n`。
- `month_rules`: 异常行数 = 0（PASS）
- `xun_rules`: 异常行数 = 0（PASS）
- `week_rules`: 异常行数 = 0（PASS）

### 5.4 【验收 4】数字回查：规则表 vs 明细重算抽样
| 股票代码 | 流月柱 | 窗位 | 规则表值 (有效/涨/跌/平) | occurrences 逐条重算值 | 判定 |
|---|---|---|---|---|---|
| 600519.SH (贵州茅台) | 丙午 | 第1旬 (上旬) | 5 / 5 / 0 / 0 | 5 / 5 / 0 / 0 | PASS |
| 600519.SH (贵州茅台) | 丙午 | 第2旬 (中旬) | 5 / 3 / 2 / 0 | 5 / 3 / 2 / 0 | PASS |
| 600519.SH (贵州茅台) | 丙午 | 第1周 (第1窗) | 5 / 4 / 1 / 0 | 5 / 4 / 1 / 0 | PASS |
| 000001.SZ (平安银行) | 甲子 | 第2旬 (中旬) | 7 / 2 / 5 / 0 | 7 / 2 / 5 / 0 | PASS |
| 600036.SH (招商银行) | 庚辰 | 第3周 (第3窗) | 5 / 3 / 2 / 0 | 5 / 3 / 2 / 0 | PASS |

### 5.5 【验收 5】UI 交互端到端走通（Puppeteer 自动化）
测试脚本：`tools/xun_week_walk.js`（在临时独立测试端口 8739 上完成无污染真机走通）：
1. 默认进入页面三，旬层首屏成功加载 180 行，默认按六十甲子顺序呈现，同一柱子 3 旬呈现斑马条纹底色；
2. 输入流月柱“丙午”，切换至周层后筛选条件无缝保留，周层按 6 周正确显示丙午全部 6 窗；
3. 点击行展开侧边抽屉，成功载入「月内窗位」3 旬与 6 周卡片，历次 5 年循环矩阵数据完整呈现；
4. 抽屉内自由切换 3 旬 / 6 周视角，数据动态更新且无错。
5. 测试结束后已彻底终止测试进程。

### 5.6 【验收 6】反向断言测试
故意将一处测试样本数据修改为 `up=5, down=4, flat=0, valid=10`（即 $5+4+0 \neq 10$），校验逻辑立刻捕获并精准抛出 AssertionError，证明校验工具具备鉴别力，非恒真假检。

---

## 6. 未覆盖范围与后续说明

1. **8737 正式端口**：
   - 为严格遵守“不重启/不接管 8737 正在跑的进程，要重启单独请示”的边界红线，本次所有开发与验收均在独立工作分支 `feat/page3-xun-week-windows` 及临时独立测试端口 8739 下完成。
   - 生产 8737 端口目前未被修改或重启，若需上线到 8737，只需在指示下将 8737 指向该分支并重启即可。
2. **页面五导出与机会扫描**：
   - 页面三现有的表格导出（SheetJS）已天然支持导出现有旬层（180行）和周层（360行）池层数据；
   - 页面五（机会扫描）目前维持月柱层级的全市场扫描，未将 3 旬 / 6 周的买入卖出窗口纳入自动选股排序。此功能可作为下一阶段规划。
