"""抖音：用 yt-dlp 抓视频（它自己处理签名），失败再退回解析分享页。"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
YTDLP = "/opt/dsh-qq/venv/bin/yt-dlp"


def _newest(dest: Path, pattern: str) -> Path | None:
    files = sorted(dest.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def download(url: str, dest: Path, max_mb: int = 200) -> tuple[dict, Path] | None:
    dest.mkdir(parents=True, exist_ok=True)

    # 1) yt-dlp
    try:
        tpl = str(dest / "dy_%(id)s.%(ext)s")
        subprocess.run(
            [YTDLP, "--no-warnings", "--no-playlist",
             "-f", "mp4/best", "--write-info-json",
             "-o", tpl, url],
            timeout=180, check=False, capture_output=True,
        )
        info = _newest(dest, "dy_*.info.json")
        video = _newest(dest, "dy_*.mp4")
        if video and video.stat().st_size > 10 * 1024:
            meta = {}
            if info:
                try:
                    meta = json.loads(info.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    meta = {}
                info.unlink(missing_ok=True)
            if video.stat().st_size > max_mb * 1024 * 1024:
                video.unlink(missing_ok=True)
                return None
            return {
                "title": meta.get("title") or "抖音视频",
                "up": meta.get("uploader") or meta.get("creator") or "",
                "desc": (meta.get("description") or "")[:500],
                "duration": int(meta.get("duration") or 0),
                "tname": "抖音",
                "view": meta.get("view_count") or 0,
                "quality": "yt-dlp",
                "bvid": meta.get("id") or "",
            }, video
    except Exception:  # noqa: BLE001
        pass

    # 2) 退回分享页解析
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA_MOBILE})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'"play_addr":\{[^}]*"url_list":\["([^"]+)"', html)
        if not m:
            m = re.search(r'playAddr":"([^"]+)"', html)
        if not m:
            return None
        play = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
        out = dest / "dy_fallback.mp4"
        req2 = urllib.request.Request(play, headers={"User-Agent": UA_MOBILE, "Referer": "https://www.douyin.com/"})
        with urllib.request.urlopen(req2, timeout=60) as r2, out.open("wb") as fh:
            total = 0
            while True:
                chunk = r2.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    out.unlink(missing_ok=True)
                    return None
                fh.write(chunk)
        return {
            "title": "抖音视频", "up": "", "desc": "", "duration": 0,
            "tname": "抖音", "view": 0, "quality": "分享页", "bvid": "",
        }, out
    except Exception:  # noqa: BLE001
        return None
