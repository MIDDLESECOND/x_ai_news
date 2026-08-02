# -*- coding: utf-8 -*-
"""L3：按量付费 X API 定时拉取 List 时间线。默认不启用。

启用前提（见 docs/frontier-radar项目指南.md §7）：
  - L2 月均触发 ≥ 8 次，且多数只是"拉 List 时间线看看有无新动静"这类低判断力操作；
  - 用官方按量付费 API（读约 $0.005/条，价格以 Developer Console 实时为准）；
  - 每周 2-3 次、20-50 账号，月成本控制在 $10-30；
  - 密钥走 .env（X_API_KEY），不入库。

明确禁止：任何使用账号 cookie 的无头爬虫（twscrape 等）、任何绕过官方接口的定时抓取。

实施要点（时机到了再写）：
  - 读取 X List ID（List 在 X 网页端手动维护，成员 = accounts.yaml 的 seed）；
  - GET /2/lists/{id}/tweets（时间线端点无搜索的 7 天窗口限制）；
  - 结果落 data/raw/YYYY-MM-DD/x_list.json，与 L1 同构，build_digest.py 直接消化。
"""
import os
import sys

if os.environ.get("ENABLE_L3") != "1":
    sys.exit("L3 默认关闭。满足 §7 前提后设置 ENABLE_L3=1 并补全实现。")

raise NotImplementedError("Phase 4：满足启用前提后实施。")
