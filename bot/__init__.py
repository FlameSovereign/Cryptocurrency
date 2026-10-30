"""土制量化梭哈机器人。

模块分工：
- candles / indicators：K 线和技术指标（纯 Python，无 numpy 依赖）
- strategies：策略注册表，一个文件一个流派，自动发现
- wallet / engine：虚拟钱包 + 决策引擎（实盘、模拟盘、回测共用同一套逻辑）
- backtest：回测与指标统计
- data：行情获取（ccxt 公共接口）和本地缓存
- brokers：模拟盘撮合 / 实盘下单
- run：GitHub Actions 每次触发时执行的入口
"""
