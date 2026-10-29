#!/usr/bin/env python3
"""它自己的闹钟：给自己排任务，到点由桥接叫醒。

用法:
    sched.py add "明天 08:30" "问他起床没"
    sched.py add "+2h" "自己刷会儿B站"
    sched.py add "23:00" --repeat daily "写今天的日记"
    sched.py list
    sched.py del t1
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

STATE = Path("/opt/dsh-qq/workspace/.state/schedule.json")


def load() -> list[dict]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save(items: list[dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def parse_when(text: str) -> int:
    """把各种写法换成时间戳。"""
    now = datetime.now()
    t = text.strip()

    m = re.fullmatch(r"\+(\d+)([smhd])", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return int((now + delta).timestamp())

    t = t.replace("明天", "").replace("今天", "").strip()
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int(target.timestamp())

    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
        try:
            return int(datetime.strptime(t, fmt).timestamp())
        except ValueError:
            continue

    raise ValueError(f"看不懂的时间：{text}（可用 +2h / 23:00 / 明天 08:30 / 2026-10-28 08:30）")


def add(note: str, when: str, repeat: str | None = None) -> dict:
    items = load()
    item = {
        "id": uuid.uuid4().hex[:8],
        "due": parse_when(when),
        "note": note.strip()[:300],
        "repeat": repeat,
        "created": int(time.time()),
    }
    items.append(item)
    items.sort(key=lambda x: x["due"])
    save(items[:60])
    return item


DELETED = STATE.parent / "sched-deleted.json"


def _read_deleted() -> list[str]:
    try:
        return json.loads(DELETED.read_text(encoding="utf-8"))
    except Exception:
        return []


def _mark_deleted(item_id: str) -> None:
    """记墓碑：删掉的任务不许被 complete() 又排回来。"""
    try:
        ids = _read_deleted()
        if item_id not in ids:
            ids.append(item_id)
        DELETED.parent.mkdir(parents=True, exist_ok=True)
        DELETED.write_text(json.dumps(ids[-300:]), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def delete(item_id: str) -> bool:
    items = load()
    kept = [i for i in items if i["id"] != item_id]
    if len(kept) == len(items):
        return False
    save(kept)
    _mark_deleted(item_id)
    return True


def due(now: int | None = None) -> list[dict]:
    now = int(now or time.time())
    return [i for i in load() if int(i["due"]) <= now]


def _step_seconds(rep: str) -> int:
    """支持 20m / 2h / 90s / hourly / daily / weekly。"""
    rep = (rep or "").strip().lower()
    if rep.endswith("m") and rep[:-1].isdigit():
        return int(rep[:-1]) * 60
    if rep.endswith("h") and rep[:-1].isdigit():
        return int(rep[:-1]) * 3600
    if rep.endswith("s") and rep[:-1].isdigit():
        return int(rep[:-1])
    return {"hourly": 3600, "daily": 86400, "weekly": 604800}.get(rep, 86400)


def complete(item: dict) -> None:
    """一次性任务删掉；重复任务排下一次。

    两种情况下不再排下一次（防“删了又回来”）：
      1. 这条任务已经被删掉（有墓碑）
      2. 任务表里已经找不到它（说明在响的过程中被删了）
    """
    iid = item.get("id")
    items = load()

    if iid in _read_deleted():
        items = [i for i in items if i["id"] != iid]
        save(items)
        return

    still_there = any(i["id"] == iid for i in items)
    items = [i for i in items if i["id"] != iid]

    rep = item.get("repeat")
    if rep and still_there:
        step = _step_seconds(rep)
        nxt = item.copy()
        nxt["due"] = int(item["due"]) + step
        while nxt["due"] <= time.time():
            nxt["due"] += step
        items.append(nxt)

    items.sort(key=lambda x: x["due"])
    save(items)


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]

    if cmd == "add":
        rest = [a for a in args[1:] if not a.startswith("--")]
        repeat = None
        for a in args[1:]:
            if a.startswith("--repeat"):
                repeat = a.split("=", 1)[1] if "=" in a else None
        if repeat is None and "--repeat" in args:
            idx = args.index("--repeat")
            if idx + 1 < len(args):
                repeat = args[idx + 1]
                rest = [a for a in rest if a != repeat]
        if len(rest) < 2:
            print("用法: add <时间> <要做什么>")
            return 1
        item = add(" ".join(rest[1:]), rest[0], repeat)
        print(f"排好了 {item['id']} → {fmt(item['due'])}：{item['note']}")
        return 0

    if cmd == "list":
        items = load()
        if not items:
            print("(没有待办)")
            return 0
        for i in items:
            flag = f" 每{'天' if i.get('repeat')=='daily' else i.get('repeat')}" if i.get("repeat") else ""
            print(f"{i['id']}  {fmt(i['due'])}{flag}  {i['note']}")
        return 0

    if cmd == "del":
        if len(args) < 2:
            print("用法: del <id>")
            return 1
        print("删掉了" if delete(args[1]) else "没找到这个 id")
        return 0

    print(f"不认识的命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
