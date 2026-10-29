#!/usr/bin/env python3
"""每日自省 + 长期记忆维护。

读最近的聊天记录，产出两类东西：
  F: 关于对方的事实 → memory/facts.md（无上限，长期保存）
  S: 说话经验       → AGENTS.md 的「我学到的」（注入层，限 20 条，溢出的归档）

由 systemd timer 每晚 04:17 跑，也可以 QQ 里发 /reflect 手动触发。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time

from deepseek_harness import DeepSeekHarness

ROOT = pathlib.Path("/opt/dsh-qq")
WORKSPACE = ROOT / "workspace"
MEMORY = WORKSPACE / "memory"
AGENTS = WORKSPACE / "AGENTS.md"
FACTS = MEMORY / "facts.md"
AUTO = MEMORY / "facts-auto.md"
ARCHIVE = MEMORY / "style-archive.md"
CHATLOG = WORKSPACE / ".chatlog"

MARK = "## 我学到的"
MAX_ITEMS = 20
PLACEHOLDER = "（还没有，慢慢来）"


def ensure_dirs() -> None:
    MEMORY.mkdir(parents=True, exist_ok=True)
    if not FACTS.exists():
        FACTS.write_text("# 关于对方\n\n", encoding="utf-8")
    if not ARCHIVE.exists():
        ARCHIVE.write_text("# 旧的说话笔记（归档）\n\n", encoding="utf-8")


def read_learned() -> list[str]:
    if not AGENTS.exists():
        return []
    text = AGENTS.read_text(encoding="utf-8")
    if MARK not in text:
        return []
    tail = text.split(MARK, 1)[1]
    items = [ln.strip()[2:].strip() for ln in tail.splitlines() if ln.strip().startswith("- ")]
    return [x for x in items if x and x != PLACEHOLDER]


def write_learned(items: list[str]) -> None:
    text = AGENTS.read_text(encoding="utf-8")
    head = text.split(MARK, 1)[0].rstrip()
    body = "\n".join(f"- {x}" for x in items) if items else f"- {PLACEHOLDER}"
    AGENTS.write_text(f"{head}\n\n{MARK}\n\n{body}\n", encoding="utf-8")


def archive_style(items: list[str]) -> None:
    if not items:
        return
    stamp = time.strftime("%Y-%m-%d")
    with ARCHIVE.open("a", encoding="utf-8") as fh:
        for x in items:
            fh.write(f"- {stamp} {x}\n")


def load_recent(limit: int = 120) -> list[dict]:
    records: list[dict] = []
    for f in sorted(CHATLOG.glob("*.jsonl"))[-3:]:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records[-limit:]


def facts_existing() -> str:
    try:
        return FACTS.read_text(encoding="utf-8")
    except Exception:
        return ""


def append_facts(items: list[str]) -> int:
    existing = facts_existing()
    stamp = time.strftime("%Y-%m-%d")
    added = 0
    with FACTS.open("a", encoding="utf-8") as fh:
        for item in items:
            key = re.sub(r"\s+", "", item)[:12]
            if key and key in re.sub(r"\s+", "", existing):
                continue
            fh.write(f"- {stamp} {item}\n")
            added += 1
    return added


def prune_reflect_sessions(keep: int = 10) -> None:
    """自省会话每次都是新的，只留最近几条。"""
    root = ROOT / "home-reflect" / "sessions"
    if not root.exists():
        return
    dirs = sorted(root.glob("*/reflect-*"), key=lambda p: p.name)
    for d in dirs[:-keep]:
        try:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
        except Exception:
            pass


def main() -> int:
    ensure_dirs()
    prune_reflect_sessions()
    records = load_recent()
    if len(records) < 12:
        print(f"记录太少（{len(records)}），跳过")
        return 0

    convo = "\n".join(f"对方：{r['user']}\n我：{r['bot']}" for r in records)
    current = read_learned()
    known_facts = facts_existing()[-8000:]
    try:
        auto_facts = AUTO.read_text(encoding="utf-8").strip()[-4000:]
    except Exception:
        auto_facts = ""

    prompt = f"""下面是一个人和朋友的聊天记录。

【聊天记录】
{convo}

【已经知道的事实】
{known_facts}

【自动摘录（机器从对话里挑的，可能有错）】
{auto_facts or "（这次没有）"}

上面这些是自动摘的。你判断：哪些是**真的**、值得记？写成 F: 行。
错的、太琐碎的、重复的，忽略就行。

【已经记下的说话经验】
{chr(10).join('- ' + x for x in current) if current else '（无）'}

请输出两类内容，每行以标记开头，不要编号、不要解释：

F: 关于对方的**新**事实（名字、职业、住哪、家人、喜好、正在做的事、承诺），一行一条，20 字内。
   只写聊天里明确说过的，不要猜。没有就完全不输出 F 行。

S: **新的**说话经验（他喜欢什么语气、哪里回得不像人、下次怎么说），一行一条，20 字内。
   没有就完全不输出 S 行。

最多各 3 条，宁可少写。输出示例：
F: 他在广州做后端
S: 他喜欢被叫全哥
"""

    # sdk-minimal 的会话压缩格式和主 profile 不同，必须用独立 home
    sid = f"reflect-{time.strftime('%Y%m%d-%H%M%S')}"
    with DeepSeekHarness(
        dsh_home=str(ROOT / "home-reflect"),
        cwd=str(WORKSPACE),
        provider="deepseek-official",
        model="deepseek-v4-flash",
        profile="sdk-minimal",
    ) as h:
        result = h.run(prompt, session_id=sid)

    raw = (result.final_response or "").strip()
    print("原始输出:", raw.replace("\n", " | ")[:500])

    new_facts: list[str] = []
    new_style: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r"^[Ff]\s*[:：]\s*(.+)$", line)
        if m:
            new_facts.append(m.group(1).strip()[:40])
            continue
        m = re.match(r"^[Ss]\s*[:：]\s*(.+)$", line)
        if m:
            new_style.append(m.group(1).strip()[:40])

    added_facts = append_facts([x for x in new_facts if x])

    # 处理过就把自动摘录归档，避免下次重复判断
    if auto_facts and added_facts > 0:
        try:
            with (MEMORY / "facts-auto-archive.md").open("a", encoding="utf-8") as fh:
                fh.write(auto_facts.rstrip() + "\n")
            AUTO.write_text("", encoding="utf-8")
        except Exception:
            pass

    merged = current[:]
    for item in new_style:
        if not any(item == x or item in x or x in item for x in merged):
            merged.append(item)

    overflow: list[str] = []
    if len(merged) > MAX_ITEMS:
        overflow = merged[: len(merged) - MAX_ITEMS]
        merged = merged[-MAX_ITEMS:]
    archive_style(overflow)
    write_learned(merged)

    print(f"事实 +{added_facts} 条；风格新增 {len(new_style)} 条，归档 {len(overflow)} 条，共 {len(merged)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
