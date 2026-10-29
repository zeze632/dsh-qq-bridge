#!/usr/bin/env python3
"""B 站命令行工具：给模型自己调用，让它真的能"自己上 B 站"。

用法:
    bili_cli.py hot                # 热门榜前 15 个（标题+时长+bvid）
    bili_cli.py search 关键词       # 搜索前 15 个
    bili_cli.py zone 游戏           # 某分区排行前 15 个
    bili_cli.py watch BV号          # 下载这个视频，抽帧+转文字，输出 JSON
    bili_cli.py comments BV号       # 看热评
    bili_cli.py post BV号 内容      # 发一条评论
    bili_cli.py del BV号 rpid       # 删自己的评论
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/dsh-qq/bridge")

import bili  # noqa: E402
import bridge  # noqa: E402

DEST = Path("/opt/dsh-qq/workspace/.inbox")


def brief(v: dict) -> dict:
    return {
        "bvid": v.get("bvid"),
        "title": (v.get("title") or "").replace('<em class="keyword">', "").replace("</em>", ""),
        "up": (v.get("owner") or {}).get("name") or v.get("author") or "",
        "duration": v.get("duration"),
        "play": (v.get("stat") or {}).get("view") or v.get("play") or 0,
        "desc": (v.get("desc") or "")[:120],
    }


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd, rest = args[0], args[1:]

    if cmd == "hot":
        items = [brief(v) for v in bili.popular(30)][:15]
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0

    if cmd == "search":
        if not rest:
            print("缺少关键词")
            return 1
        items = [brief(v) for v in bili.search(" ".join(rest))][:15]
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0

    if cmd == "zone":
        name = rest[0] if rest else "游戏"
        rid = next((k for k, v in bili.ZONES.items() if v == name), 4)
        items = [brief(v) for v in bili.ranking(rid, 30)][:15]
        print(json.dumps({"zone": name, "items": items}, ensure_ascii=False, indent=1))
        return 0

    if cmd == "comments":
        if not rest:
            print("缺少 BV 号")
            return 1
        items = bili.comments(rest[0], limit=int(rest[1]) if len(rest) > 1 else 20)
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0

    if cmd == "post":
        if len(rest) < 2:
            print("用法: post BV号 评论内容")
            return 1
        res = bili.post_comment(rest[0], " ".join(rest[1:]))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if cmd == "del":
        if len(rest) < 2:
            print("用法: del BV号 rpid")
            return 1
        res = bili.delete_comment(rest[0], int(rest[1]))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if cmd == "watch":
        if not rest:
            print("缺少 BV 号")
            return 1
        bvid = rest[0]
        meta, path = bili.fetch_bvid(bvid, DEST)
        frames, transcript, sheet = bridge.process_video(str(path), DEST)
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({
            **meta,
            "sheet": str(sheet) if sheet else "",
            "frames": [str(f) for f in frames],
            "transcript": transcript[:2000],
            "hint": "先跑 /opt/dsh-qq/describe.sh 看 sheet，需要细节再看 frames，结合 transcript，然后跟我说说这视频。",
        }, ensure_ascii=False, indent=1))
        return 0

    print(f"不认识的命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
