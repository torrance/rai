"""Read JSON with comments and trailing commas (OpenCode's opencode.jsonc)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def strip_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def loads(text: str) -> Any:
    cleaned = strip_comments(text)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def load(path: Path) -> Any:
    return loads(path.read_text(encoding="utf-8"))
