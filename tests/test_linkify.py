# -*- coding: utf-8 -*-
"""_linkify 不得把非 URL 的出处伪造成可点链接。

纪律 1 要求"具名观点必须能指回原始出处（可点链接）"。一个点不开的 https 链接比
没有链接更糟：它让未经核实的转述看起来像已核实的一手出处。曾有 7 条形如
'data/state/x.json' 的本地路径被渲染成 https://data/state/x.json 出现在简报里。

运行：python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_digest import _linkify  # noqa: E402


class LinkifyTest(unittest.TestCase):
    def test_absolute_urls_pass_through(self):
        for u in ("https://openai.com/index/ten-advances-in-mathematics/",
                  "http://example.com/a"):
            self.assertEqual(_linkify(u), u)

    def test_bare_host_paths_become_https(self):
        self.assertEqual(_linkify("x.com/user/status/1"), "https://x.com/user/status/1")
        self.assertEqual(_linkify("github.com/openai/ten-proofs"),
                         "https://github.com/openai/ten-proofs")

    def test_bare_domains_without_path_are_kept(self):
        """回归（假阴性方向）：账本里 codexradar.com / composio.dev / linux.do
        都是真实可达的具名出处，早先因"必须含斜杠"被整条丢弃，等于漏引。"""
        for host in ("codexradar.com", "composio.dev", "linux.do", "artificialanalysis.ai"):
            self.assertEqual(_linkify(host), f"https://{host}", host)

    def test_uppercase_scheme_is_recognised(self):
        self.assertEqual(_linkify("HTTPS://example.com/x"), "HTTPS://example.com/x")

    def test_local_relative_paths_are_rejected(self):
        """回归：首段无点 = 不是主机名，不得伪造成链接。"""
        for p in ("data/state/probe_history.json",
                  "data/state/calibration.json",
                  "reports/2026-08-01-报告.md"):
            self.assertIsNone(_linkify(p), f"{p} 不应被当作 URL")

    def test_windows_paths_are_rejected(self):
        # 用中性路径：公开层不留任何私有仓库/目录结构的痕迹（守则「私人职业信息只进私有层」）
        self.assertIsNone(_linkify(r"C:\Users\someone\notes\memo.md"))

    def test_prose_and_empty_are_rejected(self):
        for s in ("", "   ", "arxiv 2606.19348", "财联社 2026-07 中下旬", "AlphaSignalAI",
                  "智东西转述", "cjzafir 回复串"):
            self.assertIsNone(_linkify(s), s)

    def test_bare_filenames_are_not_mistaken_for_hosts(self):
        """放开"必须含斜杠"之后的新风险：像域名的裸文件名不得被当成主机。"""
        for s in ("note.md", "报告.md", "calibration.json", "D:\\x\\y.md"):
            self.assertIsNone(_linkify(s), s)


if __name__ == "__main__":
    unittest.main()
