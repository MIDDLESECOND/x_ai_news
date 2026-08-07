# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class PublicAttributionTest(unittest.TestCase):
    def test_readme_links_public_acknowledgements(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)", readme)

    def test_design_references_are_specific_and_keep_code_reuse_boundary(self):
        acknowledgements = (ROOT / "ACKNOWLEDGEMENTS.md").read_text(
            encoding="utf-8")
        for repository in (
                "https://github.com/miniflux/v2",
                "https://github.com/dgtlmoon/changedetection.io",
                "https://github.com/samuelclay/NewsBlur",
                "https://github.com/huginn/huginn"):
            self.assertIn(repository, acknowledgements)
        self.assertIn("没有已知从下列项目直接复制、翻译或改写的源码", acknowledgements)
        self.assertIn("上游版本或提交", acknowledgements)
        self.assertIn("对应文件附近保留可追溯标注", acknowledgements)


if __name__ == "__main__":
    unittest.main()
