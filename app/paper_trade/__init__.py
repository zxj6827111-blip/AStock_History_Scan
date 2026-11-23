"""生存者报告前向模拟盘（雪球组合，easytrader 适配）。

模块边界（2026-11-23 实施计划）：
- 信号只读生成器 stdout 指向的归档报告；绝不轮询 latest.json 下单；
- 唯一外部写入口：xueqiu.py 的全篮子调仓（三重开关：mode=live + 环境总闸 + 已确认基线）；
- 私有数据（Cookie/组合代码/账本）在 app/data/paper_trade/（不入 Git）；
- 公开只读快照在 data/paper_public/（脱敏，随更新包下发客户机）；
- easytrader 惰性 import：缺依赖只影响交易，不影响页面/报告/更新包主链。
"""
