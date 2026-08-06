# -*- coding: utf-8 -*-
"""HTML 探针只对可读正文做稳定变更检测。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_l1 as fl  # noqa: E402


class Response:
    def __init__(self, html):
        self.text = html
        self.content = html.encode("utf-8")
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class HtmlStubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = fl.STATE_DIR
        fl.STATE_DIR = Path(self.tmp.name)
        self.src = {
            "id": "pricing", "name": "定价页", "url": "https://example.com/pricing",
            "content_required_patterns": [r"[$￥¥]|USD|CNY|元"],
        }

    def tearDown(self):
        fl.STATE_DIR = self.old_state
        self.tmp.cleanup()

    def fetch(self, html):
        with patch.object(fl, "http_get", return_value=Response(html)):
            return fl.fetch_html_stub(self.src)[0]

    def test_script_churn_does_not_report_visible_page_change(self):
        first = self.fetch("<p>Pro $20</p><script>build=1</script>")
        second = self.fetch("<p>Pro $20</p><script>build=2</script>")
        self.assertTrue(first["readable"])
        self.assertFalse(second["changed"])

    def test_unreadable_page_reports_once_not_on_every_shell_change(self):
        first = self.fetch("<nav>Pricing</nav><script>build=1</script>")
        second = self.fetch("<nav>Pricing</nav><footer>new shell</footer>")
        self.assertFalse(first["readable"])
        self.assertIn("页面不可读", first["title"])
        self.assertFalse(second["changed"])

    def test_readability_recovery_is_reported(self):
        self.fetch("<nav>Pricing</nav>")
        recovered = self.fetch("<p>Pro $20</p>")
        self.assertTrue(recovered["changed"])
        self.assertTrue(recovered["readable"])
        self.assertIn("恢复可读", recovered["title"])

    def test_required_content_after_head_cutoff_is_emitted(self):
        item = self.fetch("<nav>" + ("navigation " * 700) + "</nav><p>Pro plan $20 monthly</p>")
        self.assertTrue(item["readable"])
        self.assertIn("Pro plan $20 monthly", item["summary"])
        self.assertLessEqual(len(item["summary"]), 5000)

    def test_markdown_mode_preserves_component_table_attributes(self):
        self.src["content_format"] = "markdown"
        self.src["content_required_patterns"] = [r"[$￥¥]", "kimi-k3"]
        item = self.fetch('<DocTable rows={[[`kimi-k3`,`¥100.00`]]} />')
        self.assertTrue(item["readable"])
        self.assertIn("¥100.00", item["summary"])


if __name__ == "__main__":
    unittest.main()
