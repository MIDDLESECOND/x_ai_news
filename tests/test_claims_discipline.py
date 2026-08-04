# -*- coding: utf-8 -*-
"""把 AGENTS.md 的纪律变成机器可检的断言，而不是只写在文档里。

覆盖：
  纪律 1（归因检查）—— 指不回可点出处的悬案不得挂 leaning/resolved 状态；
  职业含义纪律 2（三选一标注）—— 挂 ledger_ref 的悬案必须逐条标注含义类型；
  职业含义纪律 4（open 不得当硬门槛）—— open 悬案不得给出"不投某类岗位"式建议。

三组数据都要检：
  fixtures/claims_compliant.yaml —— 阳性对照，必须零违规；
  fixtures/claims_violating.yaml —— 阴性对照，必须逐条抓到（不会失败的检查等于没有检查）；
  config/claims.yaml            —— 真账本，私有文件，存在时才检。

运行：python -m unittest discover -s tests
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLAIMS = ROOT / "config" / "claims.yaml"
LEGAL_TAGS = ("confirmed fact", "inference", "personal action")
COMMITTED = ("leaning-yes", "leaning-no", "resolved")   # 等于"倾向下判断"的状态
HARD_GATE_WORDS = ("不投", "不申请", "不要投", "放弃该领域")
# 禁止性说明会把被禁的说法**引起来**（「不投某类岗位」），真正的违规建议不会。
# 早先用"前 12 个字符内是否出现『不得』"来豁免，那是个凭空定的窗口，句子稍长就误报。
_QUOTED = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"|'[^']*'")


def load(path):
    import yaml
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("claims", [])


# --- 三条纪律各自实现为纯函数，返回违规说明列表（空 = 合规）---

def violations_committed_without_source(claims):
    from build_digest import _linkify
    return [f"{c['id']}: 状态 {c['status']} 却无任何可点出处"
            for c in claims
            if c.get("status") in COMMITTED
            and not any(_linkify(ev.get("link")) for ev in c.get("evidence", []))]


def violations_missing_career_tags(claims):
    out = []
    for c in claims:
        if "ledger_ref" not in c:
            continue
        ci = c.get("career_implication")
        if not ci:
            out.append(f"{c['id']}: 挂了 ledger_ref 却无 career_implication")
            continue
        for line in ci:
            # 全角/半角冒号都接受：分隔符写法不该伪装成"非法标签"报出来
            tag = re.split(r"[：:]", line, maxsplit=1)[0].strip()
            if tag not in LEGAL_TAGS:
                out.append(f"{c['id']}: 未用三选一标注 -> {line[:30]}")
    return out


def violations_hard_gate_on_open(claims):
    out = []
    for c in claims:
        if c.get("status") != "open":
            continue
        text = " ".join(c.get("career_implication") or []) + " " + str(c.get("watch", ""))
        quoted = [(m.start(), m.end()) for m in _QUOTED.finditer(text)]
        for word in HARD_GATE_WORDS:
            # 必须遍历全部出现位置：只看第一处的话，一句合法的禁止性说明
            # 会把排在它后面的真正违规建议一起放行。
            for m in re.finditer(re.escape(word), text):
                if any(s <= m.start() < e for s, e in quoted):
                    continue        # 被引号括起来 = 在陈述"不该这么说"，不是在这么建议
                out.append(f"{c['id']}: open 悬案出现硬门槛措辞 -> {word}")
                break
    return out


RULES = (
    ("纪律1 可点出处", violations_committed_without_source),
    ("纪律2 三选一标注", violations_missing_career_tags),
    ("纪律4 无硬门槛", violations_hard_gate_on_open),
)


class CompliantFixtureTest(unittest.TestCase):
    """阳性对照：合规样本上三条纪律都不得报违规。"""

    def test_no_violations(self):
        claims = load(FIXTURES / "claims_compliant.yaml")
        self.assertTrue(claims, "样本不应为空")
        for name, rule in RULES:
            with self.subTest(rule=name):
                self.assertEqual(rule(claims), [])


class ViolatingFixtureTest(unittest.TestCase):
    """阴性对照：违规样本上每条纪律都必须抓到对应的那一条。"""

    @classmethod
    def setUpClass(cls):
        cls.claims = load(FIXTURES / "claims_violating.yaml")

    def test_catches_committed_without_source(self):
        found = violations_committed_without_source(self.claims)
        self.assertTrue(any("violates-committed-without-source" in v for v in found), found)

    def test_catches_missing_career_tags(self):
        found = violations_missing_career_tags(self.claims)
        self.assertTrue(any("violates-missing-career-tags" in v for v in found), found)

    def test_catches_hard_gate_beyond_first_match(self):
        """回归：违规句排在一句合法的禁止性说明之后，只看第一处匹配会漏掉。"""
        found = violations_hard_gate_on_open(self.claims)
        self.assertTrue(any("violates-hard-gate-on-open-claim" in v for v in found), found)


@unittest.skipUnless(CLAIMS.exists(), "config/claims.yaml 是私有文件，公开仓库中不存在")
class RealLedgerTest(unittest.TestCase):
    """真账本必须同时满足三条纪律。"""

    def test_real_ledger_is_compliant(self):
        claims = load(CLAIMS)
        for name, rule in RULES:
            with self.subTest(rule=name):
                self.assertEqual(rule(claims), [])


if __name__ == "__main__":
    unittest.main()
