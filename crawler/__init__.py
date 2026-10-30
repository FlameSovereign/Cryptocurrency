"""爬虫：定时把公开数据记成 CSV，攒着以后做研究用。

    python -m crawler.run [--only a,b] [--data-dir data]

数据源（互相独立，一个挂了不影响别的）：
- fear_greed    恐惧贪婪指数全量历史（alternative.me），按日期去重 → fear_greed.csv
- trending      CoinGecko 热搜榜快照 → trending.csv
- gate_movers   Gate 现货 24h 涨跌榜快照（过滤掉成交额太小的币和杠杆代币）→ gate_movers.csv
- stock_tokens  Gate 美股代币（xStocks / Ondo）vs 美股本尊的价格和溢价快照 → stock_tokens.csv

只用公开接口，不需要任何密钥。参数在 config.toml 的 [crawler] 表里改，默认值写在各数据源模块的 DEFAULTS。
"""
