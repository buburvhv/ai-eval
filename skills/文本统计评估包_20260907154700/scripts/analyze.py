# -*- coding: utf-8 -*-
"""文本统计辅助脚本：从 stdin 读 JSON 上下文（含 input_text），输出客观统计到 stdout。"""
import json
import re
import sys
from collections import Counter

ctx = json.load(sys.stdin)
text = ctx.get("input_text", "")

chars = len(re.sub(r"\s", "", text))
sentences = [s for s in re.split(r"[。！？!?；;\n]", text) if s.strip()]
avg_len = round(chars / len(sentences), 1) if sentences else 0

stop = set("的了是在和我有就不人都一一个上也很到说要去你会着没")
words = [w for w in re.findall(r"[一-鿿]{2,}", text) if w not in stop]
top = Counter(words).most_common(5)

greeting = any(k in text for k in ("你好", "您好", "hi", "hello", "在吗", "嗨", "哈喽"))
questions = text.count("？") + text.count("?")

level = "短(<50字)" if chars < 50 else ("中(50-200字)" if chars < 200 else "长(>=200字)")

print(json.dumps({
    "字符数": chars,
    "句数": len(sentences),
    "平均句长": avg_len,
    "高频词": top,
    "含问候语": greeting,
    "问句数": questions,
    "文本长度级别": level,
}, ensure_ascii=False, indent=2))
