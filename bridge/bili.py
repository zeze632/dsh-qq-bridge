"""B 站：挑视频、拿流、下载。

- 没登录：buvid3 匿名 cookie + html5 流（约 360p）
- 登录后：读 /opt/dsh-qq/bili_cookies.json，走 DASH 拿 1080p（视频/音频分开，ffmpeg 合并）
"""
from __future__ import annotations

import json
import random
import subprocess
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COOKIE_JSON = Path("/opt/dsh-qq/bili_cookies.json")
_b3 = ""
_b3_ts = 0.0


def _get(url: str, cookie: str = "", referer: str = "https://www.bilibili.com/") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        **({"Cookie": cookie} if cookie else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _post(url: str, data: dict, cookie: str) -> dict:
    import urllib.parse

    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def csrf() -> str:
    """发评论要的 csrf，就是 cookie 里的 bili_jct。"""
    try:
        data = json.loads(COOKIE_JSON.read_text(encoding="utf-8"))
        return data.get("bili_jct", "")
    except Exception:
        return ""


def aid_of(bvid: str) -> int:
    view = api(f"/x/web-interface/view?bvid={bvid}")
    if view.get("code") != 0:
        raise RuntimeError(f"view 失败: {view.get('message')}")
    return int(view["data"]["aid"])


def comments(bvid: str, limit: int = 20, sort: int = 2) -> list[dict]:
    """热评。sort=2 按热度。"""
    try:
        aid = aid_of(bvid)
        d = api(f"/x/v2/reply?type=1&oid={aid}&sort={sort}&ps={limit}&pn=1")
        replies = (d.get("data") or {}).get("replies") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in replies:
        text = ((r.get("content") or {}).get("message") or "").strip()
        if not text:
            continue
        out.append({
            "user": ((r.get("member") or {}).get("uname") or "")[:20],
            "text": text[:300],
            "like": int((r.get("like") or 0)),
            "replies": int((r.get("rcount") or 0)),
        })
    return out


POST_STATE = Path("/opt/dsh-qq/workspace/.state/bili_post.json")
MAX_PER_HOUR = 20
WINDOW_SECONDS = 3600


def _recent_stamps() -> list[int]:
    """最近一小时内的发送时间戳。"""
    try:
        st = json.loads(POST_STATE.read_text(encoding="utf-8"))
    except Exception:
        return []
    now = time.time()
    return [int(t) for t in st.get("stamps", []) if now - int(t) < WINDOW_SECONDS]


def _post_quota_ok() -> tuple[bool, str]:
    """发评论节流：一小时最多 20 条。"""
    recent = _recent_stamps()
    if len(recent) >= MAX_PER_HOUR:
        wait = int((WINDOW_SECONDS - (time.time() - min(recent))) / 60) + 1
        return False, f"这一小时已经发了 {len(recent)} 条，还得等 {wait} 分钟"
    return True, ""


def _post_quota_bump() -> None:
    recent = _recent_stamps()
    recent.append(int(time.time()))
    POST_STATE.parent.mkdir(parents=True, exist_ok=True)
    POST_STATE.write_text(json.dumps({"stamps": recent[-MAX_PER_HOUR:]}), encoding="utf-8")


def post_comment(bvid: str, message: str) -> dict:
    """发一条评论。返回 {ok, rpid, error}。"""
    message = message.strip()
    if not message:
        return {"ok": False, "error": "内容为空"}
    ok, why = _post_quota_ok()
    if not ok:
        return {"ok": False, "error": why}
    token = csrf()
    if not token:
        return {"ok": False, "error": "没有 bili_jct，无法验证"}
    try:
        aid = aid_of(bvid)
        res = _post("https://api.bilibili.com/x/v2/reply/add",
                    {"oid": aid, "type": 1, "message": message, "plat": 1, "csrf": token},
                    cookie_header())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}
    if res.get("code") == 0:
        _post_quota_bump()
        return {"ok": True, "rpid": (res.get("data") or {}).get("rpid"), "aid": aid}
    return {"ok": False, "error": f"{res.get('code')}: {res.get('message')}"}


def delete_comment(bvid: str, rpid: int) -> dict:
    token = csrf()
    try:
        aid = aid_of(bvid)
        res = _post("https://api.bilibili.com/x/v2/reply/del",
                    {"oid": aid, "type": 1, "rpid": rpid, "csrf": token},
                    cookie_header())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": res.get("code") == 0, "error": res.get("message")}


def buvid3(force: bool = False) -> str:
    global _b3, _b3_ts
    if force or not _b3 or time.time() - _b3_ts > 3600:
        data = json.loads(_get("https://api.bilibili.com/x/frontend/finger/spi"))["data"]
        _b3 = data["b_3"]
        _b3_ts = time.time()
    return _b3


def cookie_header() -> str:
    """优先用登录 cookie，拼上 buvid3。"""
    parts = [f"buvid3={buvid3()}"]
    if COOKIE_JSON.exists():
        try:
            data = json.loads(COOKIE_JSON.read_text(encoding="utf-8"))
            parts += [f"{k}={v}" for k, v in data.items() if k and v]
        except Exception:  # noqa: BLE001
            pass
    return "; ".join(parts)


def logged_in() -> bool:
    return COOKIE_JSON.exists()


def api(path: str) -> dict:
    return json.loads(_get("https://api.bilibili.com" + path, cookie=cookie_header()))


def popular(limit: int = 30) -> list[dict]:
    out: list[dict] = []
    for pn in (1, 2):
        if len(out) >= limit:
            break
        try:
            out.extend(api(f"/x/web-interface/popular?ps=20&pn={pn}").get("data", {}).get("list", []))
        except Exception:  # noqa: BLE001
            break
    return out[:limit]


_BLOCK_WORDS = (
    "色情", "裸", "脱衣", "性交", "约炮", "一夜情", "做爱", "援交", "阴茎", "阴道",
    "赌博", "博彩", "私彩", "毒品", "迷奸", "偷拍",
)


def blocked(v: dict) -> bool:
    title = (v.get("title") or "") + " " + (v.get("desc") or "")
    return any(w in title for w in _BLOCK_WORDS)


ZONES = {
    4: "游戏",
    119: "鬼畜",
    36: "知识",
    3: "音乐",
    188: "科技",
    160: "生活",
    1: "动画",
    181: "影视",
    5: "娱乐",
    211: "美食",
}


def ranking(rid: int, limit: int = 40) -> list[dict]:
    try:
        d = api(f"/x/web-interface/ranking/v2?rid={rid}&type=all")
        return ((d.get("data") or {}).get("list") or [])[:limit]
    except Exception:  # noqa: BLE001
        return []


def pick(max_seconds: int = 420, min_seconds: int = 25, avoid: set[str] | None = None,
         zone: int | None = None) -> dict | None:
    avoid = avoid or set()
    source = ranking(zone) if zone else popular()
    pool = [v for v in source
            if min_seconds <= int(v.get("duration") or 0) <= max_seconds
            and v.get("bvid") not in avoid and not blocked(v)]
    if not pool and zone:
        pool = [v for v in popular()
                if min_seconds <= int(v.get("duration") or 0) <= max_seconds and v.get("bvid") not in avoid]
    if not pool:
        return None
    return random.choice(pool[:12])


def parse_duration(v: dict) -> int:
    d = v.get("duration")
    if isinstance(d, (int, float)):
        return int(d)
    parts = str(d or "0").split(":")
    try:
        nums = [int(x) for x in parts if x != ""]
    except ValueError:
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def search(keyword: str, limit: int = 30) -> list[dict]:
    import urllib.parse

    kw = urllib.parse.quote(keyword)
    try:
        d = api(f"/x/web-interface/search/type?search_type=video&keyword={kw}&page=1")
        lst = ((d.get("data") or {}).get("result") or [])[:limit]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for v in lst:
        v = dict(v)
        v["duration"] = parse_duration(v)
        v["title"] = (v.get("title") or "").replace('<em class="keyword">', "").replace("</em>", "")
        out.append(v)
    return out


def fetch_by_keyword(keyword: str, dest: Path, avoid: set[str] | None = None,
                     max_seconds: int = 420) -> tuple[dict, Path] | None:
    avoid = avoid or set()
    pool = [v for v in search(keyword)
            if 25 <= int(v.get("duration") or 0) <= max_seconds
            and v.get("bvid") not in avoid and not blocked(v)]
    if not pool:
        return None
    v = random.choice(pool[:10])
    meta, path = fetch_bvid(v["bvid"], dest)
    meta["zone"] = f"搜索:{keyword}"
    return meta, path


def fetch_many(dest_dir: Path, count: int = 2, avoid: set[str] | None = None) -> list[tuple[dict, Path]]:
    """一次刷几个：随机挑分区，各下一个。"""
    avoid = set(avoid or ())
    got: list[tuple[dict, Path]] = []
    zones = random.sample(list(ZONES), k=min(3, len(ZONES)))
    for i in range(count):
        zone = zones[i % len(zones)]
        v = pick(avoid=avoid, zone=zone)
        if not v:
            continue
        try:
            meta, path = fetch_bvid(v["bvid"], dest_dir)
            meta["zone"] = ZONES.get(zone, "")
            got.append((meta, path))
            avoid.add(v["bvid"])
        except Exception:  # noqa: BLE001
            continue
    return got


def find_cid(bvid: str) -> tuple[int, dict]:
    view = api(f"/x/web-interface/view?bvid={bvid}")
    if view.get("code") != 0:
        raise RuntimeError(f"view 失败: {view.get('message')}")
    d = view["data"]
    return int(d["cid"]), d


def streams(bvid: str, cid: int) -> tuple[str, str | None]:
    """返回 (视频地址, 音频地址)。登录了走 DASH 1080p，否则 html5 单流。"""
    if logged_in():
        p = api(f"/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=16&fourk=1")
        dash = (p.get("data") or {}).get("dash") or {}
        if dash.get("video"):
            videos = [v for v in dash["video"] if int(v.get("height") or 0) <= 1080] or dash["video"]
            videos.sort(key=lambda v: (int(v.get("height") or 0), int(v.get("bandwidth") or 0)))
            best = videos[-1]
            audios = dash.get("audio") or []
            audios.sort(key=lambda a: int(a.get("bandwidth") or 0))
            video_url = best.get("baseUrl") or best.get("base_url")
            audio_url = (audios[-1].get("baseUrl") or audios[-1].get("base_url")) if audios else None
            if video_url:
                return video_url, audio_url
    p = api(f"/x/player/playurl?bvid={bvid}&cid={cid}&qn=32&fnval=0&platform=html5")
    if p.get("code") != 0:
        raise RuntimeError(f"playurl 失败: {p.get('message')}")
    durl = p["data"].get("durl") or []
    if not durl:
        raise RuntimeError("没有可下载的流")
    return durl[0]["url"], None


def download(url: str, dest: Path, max_mb: int = 300) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    limit = max_mb * 1024 * 1024
    with urllib.request.urlopen(req, timeout=180) as r, dest.open("wb") as fh:
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"文件超过 {max_mb}MB")
            fh.write(chunk)
    return dest


def fetch_bvid(bvid: str, dest_dir: Path) -> tuple[dict, Path]:
    """按 bvid 下载指定视频。"""
    cid, detail = find_cid(bvid)
    vurl, aurl = streams(bvid, cid)

    tmp_v = dest_dir / f"{bvid}.v.m4s"
    out = dest_dir / f"{bvid}.mp4"
    download(vurl, tmp_v)

    if aurl:
        tmp_a = dest_dir / f"{bvid}.a.m4s"
        download(aurl, tmp_a)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_v), "-i", str(tmp_a),
             "-c", "copy", str(out)],
            timeout=180, check=False,
        )
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    else:
        tmp_v.rename(out)

    meta = {
        "bvid": bvid,
        "title": detail.get("title") or "",
        "up": (detail.get("owner") or {}).get("name", ""),
        "desc": (detail.get("desc") or "")[:500],
        "duration": int(detail.get("duration") or 0),
        "tname": detail.get("tname") or "",
        "view": detail.get("stat", {}).get("view", 0),
        "quality": "登录画质" if (logged_in() and aurl) else "匿名 360p",
    }
    return meta, out


def fetch_one(dest_dir: Path, avoid: set[str] | None = None) -> tuple[dict, Path] | None:
    """挑一个短视频并下载，返回 (元信息, 本地 mp4)。"""
    v = pick(avoid=avoid)
    if not v:
        return None
    bvid = v["bvid"]
    cid, detail = find_cid(bvid)
    vurl, aurl = streams(bvid, cid)

    tmp_v = dest_dir / f"{bvid}.v.m4s"
    out = dest_dir / f"{bvid}.mp4"
    download(vurl, tmp_v)

    if aurl:
        tmp_a = dest_dir / f"{bvid}.a.m4s"
        download(aurl, tmp_a)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_v), "-i", str(tmp_a),
             "-c", "copy", str(out)],
            timeout=180, check=False,
        )
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    else:
        tmp_v.rename(out)

    meta = {
        "bvid": bvid,
        "title": detail.get("title") or v.get("title") or "",
        "up": (detail.get("owner") or {}).get("name", ""),
        "desc": (detail.get("desc") or "")[:500],
        "duration": int(detail.get("duration") or 0),
        "tname": detail.get("tname") or v.get("tname") or "",
        "view": detail.get("stat", {}).get("view", 0),
        "quality": "登录画质" if (logged_in() and aurl) else "匿名 360p",
    }
    return meta, out
