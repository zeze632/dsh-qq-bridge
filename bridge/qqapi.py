#!/usr/bin/env python3
"""QQ 附加能力：表情包、QQ 空间（说说/动态/评论/点赞/资料/黑名单）。

常用:
    qqapi.py sticker 图片路径            按表情发给你
    qqapi.py stickers                    看表情库
    qqapi.py say 内容                    发说说
    qqapi.py say-list                    自己的说说
    qqapi.py say-detail 说说id           说说详情+评论
    qqapi.py say-del 说说id              删说说
    qqapi.py say-comment 说说id 内容      评论
    qqapi.py say-like 说说id             点赞
    qqapi.py say-unlike 说说id           取消赞
    qqapi.py say-right 说说id 权限        改可见范围(1公开 2好友 3仅自己...)
    qqapi.py feeds [数量]                好友动态
    qqapi.py pub-feeds [数量]            空友爱看推荐
    qqapi.py profile                     看空间资料
    qqapi.py set-profile 名称 [说明]      改空间名称/说明
    qqapi.py ban QQ号 [off]              拉黑/解除
    qqapi.py api 动作名 '{"参数":...}'    直接调任意 OneBot 动作
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

CFG = json.load(open("/opt/dsh-qq/bridge/config.json"))
TOKEN = CFG["onebot"].get("access_token") or ""
TARGET = int((CFG.get("proactive") or {}).get("qq") or 0)
STICKER_DIR = Path("/opt/dsh-qq/workspace/stickers")


async def call(action, params, echo="1"):
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with websockets.connect("ws://127.0.0.1:3001", additional_headers=headers) as ws:
        await ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
            if m.get("echo") == echo:
                return m


def show(r, limit=800):
    print(json.dumps(r, ensure_ascii=False)[:limit])
    return 0 if r.get("status") == "ok" else 1


async def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]

    if cmd == "stickers":
        files = sorted(STICKER_DIR.glob("*")) if STICKER_DIR.exists() else []
        print(f"(共 {len(files)} 张)" if files else f"(表情包目录是空的：{STICKER_DIR})")
        for f in files[:80]:
            print(" ", f.name)
        return 0

    if cmd in ("sticker", "image"):
        p = Path(args[1]) if len(args) > 1 else None
        if not p:
            print("用法: qqapi.py sticker 图片路径")
            return 1
        if not p.is_absolute():
            p = Path("/opt/dsh-qq/workspace") / p
        if not p.is_file():
            print(f"找不到图片: {p}")
            return 1
        data = {"file": "base64://" + base64.b64encode(p.read_bytes()).decode()}
        if cmd == "sticker":
            data["sub_type"] = 1
        return show(await call("send_private_msg", {"user_id": TARGET,
                                                    "message": [{"type": "image", "data": data}]}), 200)

    if cmd == "send":
        if len(args) < 3:
            print("用法: qqapi.py send group 群号 内容 / qqapi.py send user QQ号 内容")
            return 1
        kind, target, content = args[1], args[2], " ".join(args[3:])
        if kind == "group":
            return show(await call("send_group_msg", {
                "group_id": int(target),
                "message": [{"type": "text", "data": {"text": content}}]}), 200)
        if kind == "user":
            return show(await call("send_private_msg", {
                "user_id": int(target),
                "message": [{"type": "text", "data": {"text": content}}]}), 200)
        print("第一个参数只能是 group 或 user")
        return 1

    if cmd == "say":
        return show(await call("send_qzone_msg", {"content": " ".join(args[1:])}), 300)

    if cmd == "say-list":
        r = await call("get_qzone_msg_list", {"pos": 0, "num": int(args[1]) if len(args) > 1 else 5})
        data = r.get("data") or {}
        items = data.get("msglist") or data.get("messages") or []
        if not items:
            print("(还没有说说)")
            return 0
        for it in items:
            print(it.get("tid"), "|", (it.get("content") or "")[:70])
        return 0

    if cmd == "say-detail":
        return show(await call("get_qzone_msg_detail", {"tid": args[1]}), 1200)

    if cmd == "say-del":
        return show(await call("delete_qzone_msg", {"tid": args[1]}), 200)

    if cmd == "say-comment":
        return show(await call("comment_qzone", {"tid": args[1], "content": " ".join(args[2:])}), 300)

    if cmd == "say-like":
        return show(await call("like_qzone", {"tid": args[1]}), 200)

    if cmd == "say-unlike":
        return show(await call("unlike_qzone", {"tid": args[1]}), 200)

    if cmd == "say-right":
        return show(await call("set_qzone_msg_right", {"tid": args[1], "right": int(args[2])}), 200)

    if cmd == "feeds":
        return show(await call("get_qzone_feeds", {"num": int(args[1]) if len(args) > 1 else 5}), 1500)

    if cmd == "pub-feeds":
        return show(await call("get_qzone_public_feeds", {"num": int(args[1]) if len(args) > 1 else 5}), 1500)

    if cmd == "profile":
        return show(await call("get_qzone_profile", {}), 400)

    if cmd == "set-profile":
        params = {"name": args[1]} if len(args) > 1 else {}
        if len(args) > 2:
            params["desc"] = " ".join(args[2:])
        return show(await call("set_qzone_profile", params), 300)

    if cmd == "ban":
        if len(args) < 2:
            print("用法: qqapi.py ban QQ号 [off]")
            return 1
        enable = not (len(args) > 2 and args[2] == "off")
        return show(await call("set_qzone_ban", {"uin": int(args[1]), "enable": enable}), 200)

    if cmd == "invites":
        r = await call("get_group_system_msg", {})
        data = r.get("data") or {}
        invites = data.get("invited_requests") or []
        joins = data.get("join_requests") or []
        if not invites and not joins:
            print("(没有群邀请/加群申请)")
        for it in invites:
            print(f"[邀请] 群 {it.get('group_id')} {it.get('group_name')} 来自 {it.get('invitor_uin')} flag={it.get('invitation_seq')}")
        for it in joins:
            print(f"[申请] 群 {it.get('group_id')} {it.get('group_name')} 来自 {it.get('invitor_uin')} flag={it.get('request_seq')}")
        return 0

    if cmd in ("accept", "reject"):
        if len(args) < 2:
            print("用法: qqapi.py accept flag [invite|add]")
            return 1
        sub = args[2] if len(args) > 2 else "invite"
        return show(await call("set_group_add_request", {
            "flag": args[1], "sub_type": sub, "approve": cmd == "accept"}), 300)

    if cmd == "groups":
        r = await call("get_group_list", {})
        for g in (r.get("data") or []):
            print(g.get("group_id"), "|", g.get("group_name"), "|", g.get("member_count"), "人")
        return 0

    if cmd == "api":
        if len(args) < 2:
            print("用法: qqapi.py api 动作名 '{\"参数\":...}'")
            return 1
        params = json.loads(args[2]) if len(args) > 2 else {}
        return show(await call(args[1], params), 2000)

    print(f"不认识的命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
