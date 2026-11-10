# 流月机会表（页面五）月/旬/周三层窗口视图交付报告

## 1. 一句结论
页面五「全市场流月机会看板」已全面对齐页面三的窗口分类能力，新增 **月层（整月） / 旬层（60柱·旬） / 周层（60柱·周）** 窗口层级切换及上/中/下旬与 1–6 周子窗位快捷筛选，完整呈现窗位区间、有效窗/空窗计数、胜率、窗收益均值/中位数/最差幅度及历史月实际走势，并通过 CTE 优化使全表查询耗时从 2.4s 大幅压降至 26ms，全部 5 项深度验收与浏览器端到端走查 100% 通过。

---

## 2. 改动文件清单（绝对路径）
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/api.py`：新增 `opportunities_window()` 高性能 CTE 规则查询与字段映射，挂载 `xw` 库，统一百分比口径与十神/五行映射。
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/server.py`：在 `/api/opportunities` 路由分发参数白名单中支持 `layer` 与 `window_no`。
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/static/index.html`：在 `#board` 容器顶部新增 `#o-layer-bar`（包含 `#o-layer-seg` 3 层切换、`#o-xun-pills` 3 旬筛选、`#o-week-pills` 6 周筛选与口径红线提示）。
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/app/static/app.js`：
  - `oppFilter()` 传递 `layer` 和 `window_no`；
  - `renderOpportunity()` 针对旬层/周层动态显示窗位标签、有效窗/空窗、中位数、最差幅度等列，并处理跨窗分组样式；
  - `OPP_COL_WEIGHTS` / `OPP_HEAD_TIPS` / `OPP_EXPORT` 全面对齐新窗位字段，支持 SheetJS Excel 导出；
  - `snapshotFilters()` / `restoreFilters()` 保证刷新及前进后退时完整保留窗位与层级状态。
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/tools/opp_window_walk.js`：页面五端到端无头 Chrome 走查脚本。
- `/Volumes/workSSD/software development/AStock_History_Scan/bazi_month_relation_tengod_dev_v1/tests/test_page5_xun_week_acceptance.py`：自动化数据与口径门禁断言测试脚本。

---

## 3. 前台唯一读取路径清单
- 旬层 / 周层规则库：`/Volumes/workSSD/software development/AStock_History_Scan/allstock_xun_week_v1/database/allstock_xun_week_v1.sqlite`（`api.py` 启动只读挂载为 `xw`，仅执行只读 `SELECT`，不写临时表、不建索引）。
- 历史实际行情：`xw.xun_occurrences`（旬层 2,724,894 行）与 `xw.week_occurrences`（周层 5,449,788 行）。
- 本命盘三柱五行：`ashare.natal_profiles`。
- 命中关系特征：`ashare.stats_relation_pillar`（record_kind='OFFICIAL'）。
- 市值分档快照：`cap.cap_tier_monthly`。

---

## 4. 新增 / 修改的接口与列
### 接口更新
- `GET /api/opportunities`
  - 新增入参：
    - `layer`：`"month"`（默认）/ `"xun"`（旬层）/ `"week"`（周层）
    - `window_no`：旬层可选 `1, 2, 3`；周层可选 `1, 2, 3, 4, 5, 6`（空或不传表示全部窗口）

### 页面五表格列展示调整
| 列名 | 字段 / 计算来源 | 旬层/周层显示逻辑 |
|---|---|---|
| **名称 / 代码** | `r.name`, `r.security_code` | 始终显示，点击滑出速览抽屉 |
| **窗位及区间** | `r.window_tag` | 旬层/周层显示（如 `中旬 (10–20日)`、`第5周 (20–25日)`），带色彩徽章 |
| **生辰八字** | `p.natal_year/month/day_pillar` | 竖排展示年/月/日三柱 |
| **标的五行** | `p.*_element` | 竖排展示命盘三柱干支五行 |
| **流月(五行)** | `r.flow_pillar`, `r.stem/branch_element` | 所选流月柱及天干地支五行 |
| **有效/总窗(空)** | `ALL_valid_n / all_sample_n (ALL_invalid_n 空)` | 展示有效样本窗及空窗剔除数（空窗不入分母） |
| **涨 / 跌 / 平** | `ALL_up_n / ALL_down_n / ALL_flat_n` | 窗口内历次涨跌平次数 |
| **历史上涨/下跌比** | `ALL_up_rate` / `ALL_down_rate` | 严格按 `up_n / valid_n` 计算 |
| **实际走势** | `act.window_return`, `act.direction` | 历史月直接显示当窗实际收益与涨跌徽章；未来/本月显示「进行中」或「—」 |
| **平均涨跌** | `ALL_mean_return_pct / 100.0` | 历次窗口收益算术平均（严格按首开→末收） |
| **中位数** | `ALL_median_return_pct / 100.0` | 历次窗口收益中位数 |
| **最差幅度** | `ALL_worst_return_pct / 100.0` | 历次窗口最差一次表现 |
| **命中关系** | `relation_types` | 官方正式关系集合 |
| **市值档** | `cap_tier` | 所选流月月末快照（超大盘/大盘/中盘/小盘/微盘） |

---

## 5. 实测验收数字
执行 `python tests/test_page5_xun_week_acceptance.py` 实测结果：
1. **API 性能**：
   - 旬层 Top 100 查询耗时：**26.0 ms**（< 100 ms）
   - 周层 Top 100 查询耗时：**27.3 ms**（< 100 ms）
2. **窗位子筛选器行数过滤**：
   - 旬层 `window_no=1/2/3`：全部 100% 严格限定于指定旬位（每窗 50 行返回满格）。
   - 周层 `window_no=1..6`：全部 100% 严格限定于指定周位。
3. **计数闭合与胜率断言**：
   - `ALL_valid_n + ALL_invalid_n == all_sample_n`：100 行中 0 异常。
   - `ALL_up_n + ALL_down_n + ALL_flat_n == ALL_valid_n`：100 行中 0 异常。
   - `ALL_up_rate == round(ALL_up_n / ALL_valid_n, 6)`：100 行中 0 异常。
4. **历史月（2026-M08 丁酉）实际走势回查**：
   - 抽查 `300542.SZ`（新晨科技）第2窗（中旬）：接口实际收益 `+0.99%`、方向 `UP`，与 `xw.xun_occurrences` 库内原始日K首开末收重算值**逐值完全相等（差值 < 1e-8）**。
5. **反向断言测试**：
   - 人为给样本注入 `up+down+flat != valid` 错误，测试脚本 100% 捕获 AssertionError，证明校验非恒真。
6. **Excel 导出规范**：
   - SheetJS 导出规格包含 38 列，涵盖 `窗位及区间`、`旬/周编号`、`有效窗口数`、`空窗口数`、`应有窗口总数`、`中位数涨跌幅` 等全部新增列。

---

## 6. 未覆盖范围
- ETF 数据集暂未构建旬层与周层细分窗口规则表，当前仅全 A 股标的（5,561 只）支持三层窗口视图。若在 ETF 数据集下切至旬/周层，接口会友好提示「请切回月层查看 ETF 机会」。
- 生产环境 8737 端口进程未被触碰或重启（测试验证全量在端口 8739 独立验证完成）。
