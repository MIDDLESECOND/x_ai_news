# -*- coding: utf-8 -*-
"""brief_marker.brief_synthesized 的回归测试。

这个判定是防永久数据丢失的唯一闸门：它返回 False 就意味着允许覆盖，
而合成版日报被覆盖后无处可寻（briefs/ 不入库）。失效时全程静默——
命令照常退出 0、照常打印成功——所以必须由测试而不是肉眼来盯。

运行：python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from brief_marker import SYNTH_MARKER, brief_synthesized  # noqa: E402

MECHANICAL = (
    "# Frontier Radar 日报 — 2026-08-04\n"
    "\n"
    "> 信源：34 个成功；命中条目 135 条。\n"
    "\n"
    "## 今日发布\n"
    "- [某条目](https://example.com) — 某信源（厂商口径）\n"
)
SYNTHESIZED = MECHANICAL.replace(
    "> 信源：34 个成功；命中条目 135 条。",
    f"> 信源：34 个成功；命中条目 135 条。本期正文为{SYNTH_MARKER}（Claude Code 会话），机械链接列表见附录。",
)


class BriefSynthesizedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, newline="\n"):
        p = self.dir / "brief.md"
        # newline="" 关掉通用换行转换，让 CRLF 用例真的写出 \r\n
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("\n", newline))
        return p

    def test_missing_file_is_not_synthesized(self):
        self.assertFalse(brief_synthesized(self.dir / "不存在.md"))

    def test_mechanical_brief_is_not_synthesized(self):
        self.assertFalse(brief_synthesized(self.write(MECHANICAL)))

    def test_synthesized_brief_is_detected(self):
        self.assertTrue(brief_synthesized(self.write(SYNTHESIZED)))

    def test_crlf_line_endings_still_detected(self):
        """仓库在 Windows 上工作且 git 会把 LF 换成 CRLF——换行形态不得影响判定。"""
        self.assertTrue(brief_synthesized(self.write(SYNTHESIZED, newline="\r\n")))

    def test_marker_only_in_body_is_not_synthesized(self):
        """正文里偶然出现「人工合成」（例如某条新闻标题）不得被误判为合成版，
        否则机械版重建会被无故拒绝。"""
        body_only = MECHANICAL + f"- [谈{SYNTH_MARKER}数据的文章](https://example.com/x) — 某信源\n"
        self.assertFalse(brief_synthesized(self.write(body_only)))

    def test_long_preamble_does_not_hide_marker(self):
        """回归：旧实现只扫前 500 字符，前言变长就静默失效并覆盖合成版。
        前言长度由本仓库之外的 SKILL.md 决定，不能假设它不变。"""
        padding = "> 附注：" + "补充说明。" * 200 + "\n"   # 远超 500 字符
        long_preamble = SYNTHESIZED.replace("## 今日发布", padding + "\n## 今日发布")
        self.assertGreater(long_preamble.index("## 今日发布"), 500)
        self.assertTrue(brief_synthesized(self.write(long_preamble)))

    def test_no_heading_falls_back_to_whole_file(self):
        """没有任何 `## ` 小节时（截断/半成品）退化为全文扫描：
        宁可误判成合成版而拒绝覆盖，也不能漏判而销毁。"""
        self.assertTrue(brief_synthesized(self.write(f"# 标题\n\n> {SYNTH_MARKER}\n")))


if __name__ == "__main__":
    unittest.main()
