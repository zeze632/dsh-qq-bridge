#!/usr/bin/env python3
"""看群消息：group.py tail 群号 [条数]"""
import json
import sys
from pathlib import Path

DIR = Path("/opt/dsh-qq/workspace/.group")


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "tail":
        print("用法: group.py tail 群号 [条数]")
        return 1
    gid = args[1]
    n = int(args[2]) if len(args) > 2 else 30
    f = DIR / f"{gid}.jsonl"
    if not f.exists():
        print(f"(没收到过 {gid} 的消息)")
        return 0
    rows = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        rows.append(f"{d.get('who')}: {d.get('text')}")
    if not rows:
        print("(没有消息)")
        return 0
    print(f"最近 {len(rows)} 条：")
    for r in rows:
        print(" ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
