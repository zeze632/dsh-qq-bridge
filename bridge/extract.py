#!/usr/bin/env python3
"""自动摘录：每小时从对话索引里挑出值得长期记住的事实。

写到 memory/facts-auto.md，单独一个文件，不碰它手写的 facts.md。
每天自省时由它自己判断哪些要收进 facts.md。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path("/opt/dsh-qq")
WORKSPACE = ROOT / "workspace"
MEMORY = WORKSPACE / "memory"
INDEX = MEMORY / "chat-index.md"
AUTO = MEMORY / "facts-auto.md"
STATE = WORKSPACE / ".state" / "facts-auto.json"
MIN_NEW_LINES = 20
BATCH = 150
KEEP = 400

PROMPT = """下面是我和他今天的对话索引。请挑出**值得长期记住的事实**，只挑确定的。

值得记的：
- 他的身份、职业、住哪、作息
- 他喜欢/讨厌什么
- 他答应过的事、我答应过的事
- 发生过的重要事件（考试、比赛、生病、出行）
- 他明确说过的偏好

不该记的：闲聊、玩笑、一次性的话、我自己的发挥。

输出格式：每条一行，以 `- ` 开头，一句不超过 30 字。
没有值得记的就只输出一个 `-`（空标记）。

对话索引：
{lines}
"""


def norm(s: str) -> str:
    return re.sub(r"[\s，。、,.!！?？：:；;“”\"'（）()]+", "", s)


def main() -> int:
    if not INDEX.exists():
        print("没有索引，跳过")
        return 0
    lines = INDEX.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        cursor = json.loads(STATE.read_text(encoding="utf-8")).get("line", 0)
    except Exception:
        cursor = 0
    if cursor > len(lines):
        cursor = 0
    fresh = lines[cursor:]
    if len(fresh) < MIN_NEW_LINES:
        print(f"新对话只有 {len(fresh)} 行，不够")
        return 0

    batch = fresh[-BATCH:]
    prompt = PROMPT.format(lines="\n".join(batch))

    sys.path.insert(0, "/opt/dsh-qq/bridge")
    from deepseek_harness import DeepSeekHarness

    cfg = json.load(open("/opt/dsh-qq/bridge/config.json"))
    model = cfg["dsh"].get("model", "deepseek-flash")
    sid = f"facts-{time.strftime('%Y%m%d-%H%M%S')}"
    with DeepSeekHarness(
        dsh_home=str(ROOT / "home"),
        cwd=str(WORKSPACE),
        provider="deepseek-official",
        model=model,
    ) as h:
        h.start_session(sid)
        result = h.run(prompt, session_id=sid)

    raw = (result.final_response or "").strip()
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        item = line.lstrip("- ").strip()
        if len(item) < 4 or item == "-":
            continue
        items.append(item[:60])

    existing = ""
    if AUTO.exists():
        existing = norm(AUTO.read_text(encoding="utf-8"))
    stamp = time.strftime("%m-%d")
    added = 0
    MEMORY.mkdir(parents=True, exist_ok=True)
    with AUTO.open("a", encoding="utf-8") as fh:
        for it in items[:15]:
            key = norm(it)[:10]
            if key and key in existing:
                continue
            fh.write(f"- {stamp} {it}\n")
            existing += key
            added += 1

    # 只留最近 KEEP 行
    try:
        all_lines = AUTO.read_text(encoding="utf-8").splitlines()
        if len(all_lines) > KEEP:
            AUTO.write_text("\n".join(all_lines[-KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"line": len(lines), "at": int(time.time())}), encoding="utf-8")
    print(f"新增 {added} 条（候选 {len(items)}），游标 {len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
