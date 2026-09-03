from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def parse_json_payload(text: str, *, require: str | None = None) -> Any:
    """从模型输出里抽 JSON。

    require 给定键名时只认「含该键的顶层对象」或顶层数组，
    避免截断的输出被当成里面第一个小对象解析成功。
    """
    raw = strip_think(text)
    raw = re.sub(r"```(?:json)?", "", raw, flags=re.I).replace("```", "").strip()
    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(raw) if ch in "{["]
    for start in starts[:12]:
        try:
            value, _end = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError:
            continue
        if require is None:
            return value
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and require in value:
            return value
    raise ValueError("没有可用的 JSON")
