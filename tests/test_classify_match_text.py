# -*- coding: utf-8 -*-
"""classify() 必须为悬案匹配保留全文。

背景：item['summary'] 被截断到 300 字符是为了压缩简报与 LLM 载荷。悬案监视词一度
也匹配这个截断值，导致关键词落在第 300 字之后就永远不会被发现——真实数据上这让
全部未决悬案的命中从 146 掉到 112（约 23% 静默损失）。此处钉死两件事：
截断仍然生效（渲染侧不回退），且匹配走的是未截断的 match_text。

运行：python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_digest as bd  # noqa: E402

DAY = "2026-08-04"
# 关键词故意放在第 300 字符之后
TAIL_KEYWORD = "headcount"
LONG_SUMMARY = "填充。" * 200 + f" the team cut {TAIL_KEYWORD} last quarter"

TOPICS = {
    "model_keywords": ["Claude"],
    "topics": {"field_test": {"section": "一线实测", "keywords": ["tested"]}},
}


def payload(summary):
    return [{
        "name": "测试信源", "tier": "community",
        "items": [{
            "title": "Claude 相关条目",
            "url": "https://example.com/1",
            "summary": summary,
            "published": f"{DAY}T00:00:00+00:00",
        }],
    }]


class MatchTextTest(unittest.TestCase):
    def setUp(self):
        _, self.hits = bd.classify(payload(LONG_SUMMARY), TOPICS, DAY, 3)
        self.assertEqual(len(self.hits), 1, "前置条件：条目应当入围")
        self.item = self.hits[0]

    def test_summary_is_still_truncated(self):
        """渲染侧的截断不得回退——否则简报与 LLM 载荷会膨胀。"""
        self.assertEqual(len(self.item["summary"]), 300)
        self.assertNotIn(TAIL_KEYWORD, self.item["summary"])

    def test_match_text_keeps_full_body(self):
        self.assertIn(TAIL_KEYWORD, self.item["match_text"])

    def test_claim_watch_keyword_beyond_300_chars_is_found(self):
        """回归：这正是修复前会漏掉的信号。"""
        claims = [{"id": "t", "claim": "测试悬案", "status": "open",
                   "watch": "—", "watch_keywords": [TAIL_KEYWORD]}]
        lines = bd.claims_section(claims, self.hits, TOPICS)
        self.assertTrue(any("疑似新信号" in ln for ln in lines),
                        "落在 300 字之后的监视词应当被发现")

    def test_short_summary_unaffected(self):
        """短摘要下两者应当一致，确保改动没有引入别的行为差异。"""
        _, hits = bd.classify(payload("Claude tested today"), TOPICS, DAY, 3)
        self.assertIn(hits[0]["summary"], hits[0]["match_text"])


if __name__ == "__main__":
    unittest.main()
