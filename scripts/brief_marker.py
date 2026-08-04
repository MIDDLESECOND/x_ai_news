# -*- coding: utf-8 -*-
"""合成版日报的识别标记 —— build_digest 与 daily_orchestrator 的唯一真相源。

只依赖标准库：无人值守入口（daily_orchestrator）导入本模块不会因 yaml/requests
等第三方依赖缺失而整体挂掉，同时消除常量两处各留一份的漂移风险。
"""
from pathlib import Path

# 合成版日报开头引语行含此字样（由 daily-brief-synthesis 会话写入）。
SYNTH_MARKER = "人工合成"


def brief_synthesized(path):
    """当日日报是否已是人工合成版。

    只在**头部区块**（第一个 `## ` 小节之前）里找标记，而不是固定的前 N 字符：
    合成会话的前言长度不受本仓库控制，写死字符数会在前言变长时静默失效——
    而这个守卫失效的代价是合成版被永久覆盖（briefs/ 不入库）。
    限定在头部同时避免正文里偶然出现「人工合成」四字造成的误判。
    """
    path = Path(path)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    head = text.split("\n## ", 1)[0]
    return SYNTH_MARKER in head
