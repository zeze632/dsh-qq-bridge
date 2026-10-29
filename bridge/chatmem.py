#!/usr/bin/env python3
"""聊天记忆：索引 + 检索。

- 每轮对话写一行索引到 memory/chat-index.md
- 查的时候用中文 2 字切分打分，命中就从 .chatlog 里把原文找回来

用法:
    chatmem.py search "关键词"        # 搜索引并回原文
    chatmem.py stats                  # 索引多大、多少行
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

WORKSPACE = Path("/opt/dsh-qq/workspace")
INDEX = WORKSPACE / "memory" / "chat-index.md"
CHATLOG = WORKSPACE / ".chatlog"

_NONWORD = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def _bigrams(text: str) -> set[str]:
    text = _NONWORD.sub("", text or "")
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else {text}


def append(user: str, bot: str, key: str = "", ts: int | None = None) -> None:
    """每轮存一行索引。ts 缺省用当前时间（补历史时可传）。"""
    try:
        user = " ".join((user or "").split())[:60]
        bot = " ".join((bot or "").split())[:60]
        if not user and not bot:
            return
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or time.time()))
        with INDEX.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} | 他：{user} | 我：{bot}\n")
    except Exception:  # noqa: BLE001
        pass


def search(query: str, limit: int = 3, min_score: int = 2) -> list[dict]:
    """按 2 字重合度找最相关的几行。"""
    if not INDEX.exists() or not (query or "").strip():
        return []
    q = _bigrams(query)
    if not q:
        return []
    scored = []
    try:
        lines = INDEX.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return []
    for i, line in enumerate(lines):
        if " | " not in line:
            continue
        score = len(q & _bigrams(line))
        if score >= min_score:
            scored.append((score, i, line))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [{"score": s, "line": ln} for s, _, ln in scored[:limit]]


def full_text(line: str, max_files: int = 30) -> str:
    """拿索引里的「他：…」去 chatlog 找原文。"""
    m = re.search(r"他：(.*?) \| 我：", line)
    needle = (m.group(1)[:20] if m else "").strip()
    if not needle:
        return ""
    files = sorted(CHATLOG.glob("*.jsonl"))[-max_files:]
    for f in reversed(files):
        try:
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                u = (d.get("user") or "")
                if needle[:12] and needle[:12] in u:
                    b = (d.get("bot") or "")[:200]
                    return f"[{time.strftime('%m-%d %H:%M', time.localtime(d.get('ts', 0)))}] 他：{u[:200]}\n{' ' * 6}我：{b}"
        except Exception:  # noqa: BLE001
            continue
    return ""


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "stats":
        if not INDEX.exists():
            print("还没有索引")
            return 0
        txt = INDEX.read_text(encoding="utf-8", errors="replace")
        print(f"索引行数: {len(txt.splitlines())} | 大小: {len(txt.encode('utf-8'))/1024:.1f}KB")
        return 0
    if args[0] == "search":
        q = " ".join(args[1:])
        hits = search(q, limit=5)
        if not hits:
            print("(没找到相关的旧对话)")
            return 0
        for h in hits:
            print(f"[{h['score']}分] {h['line']}")
            ft = full_text(h["line"])
            if ft:
                print("   原文:", ft.replace("\n", "\n         "))
        return 0
    print(f"不认识的命令: {args[0]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
