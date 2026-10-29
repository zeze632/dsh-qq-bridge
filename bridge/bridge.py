#!/usr/bin/env python3
"""DSH ⇄ QQ 桥接（Linux 版）

QQ 消息 → DeepSeek Harness (Python SDK) → 回复。

除了转发，还做了一层「像人」的处理：
  · 回复按句拆成 1–3 条气泡，条与条之间模拟打字停顿
  · 整体不乱秒回，给一个随机的「思考」下限
  · 长任务先发一句「我想想」，出结果后再删掉占位
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import websockets
from deepseek_harness import DeepSeekHarness

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", BASE / "config.json"))
LOG = logging.getLogger("bridge")

HELP_TEXT = (
    "直接发消息就行。\n"
    "/new 开新会话\n"
    "/status 看当前会话\n"
    "/tasks 看它自己排的任务\n"
    "/learn 一句话 让它记一条经验\n"
    "/remember 一句话 记一条长期事实\n"
    "/memory 看它记住的事\n"
    "/persona 看它学到的说话方式\n"
    "/reflect 让它翻最近的聊天总结一下\n"
    "/bili 让它去 B 站刷视频（可带关键词，如 /bili 原神）\n"
    "/forget 撤销它上一次改人设；没有则恢复出厂\n"
    "/reset 直接恢复出厂人设\n"
    "/forget 清空自学的内容\n"
    "/help 这条帮助"
)


# ───────────────────────── 配置 ─────────────────────────


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ───────────────────────── 文本处理 ─────────────────────────

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def to_plain(text: str) -> str:
    """把 Markdown 压成 QQ 里好看的纯文本。"""
    text = ANSI.sub("", text)
    text = re.sub(r"```[a-zA-Z0-9]*\n?", "", text)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "· ", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


SENTENCE_END = re.compile(r"(?<=[。！？!?…\n])")


def split_bubbles(text: str, max_bubbles: int, min_chars: int) -> list[str]:
    """把一段话拆成几条「像人发出来的」消息。"""
    text = text.strip()
    if not text:
        return []

    sentences = [s for s in SENTENCE_END.split(text) if s.strip()]
    if len(sentences) <= 1:
        return [text]

    bubbles: list[str] = []
    buf = ""
    for s in sentences:
        buf += s
        if len(buf) >= min_chars:
            bubbles.append(buf.strip())
            buf = ""
    if buf.strip():
        if bubbles:
            bubbles[-1] += buf.strip()
        else:
            bubbles.append(buf.strip())

    if len(bubbles) > max_bubbles:
        head = bubbles[: max_bubbles - 1]
        tail = "".join(bubbles[max_bubbles - 1 :])
        bubbles = head + [tail]

    return [b for b in bubbles if b]


BILI_RE = re.compile(r"(?:bilibili\.com/video/|b23\.tv/)([A-Za-z0-9]+)")


def extract_share_links(message) -> list[dict]:
    """从 QQ 卡片（json/xml 段）里掏出标题和链接。"""
    if not isinstance(message, list):
        return []
    out: list[dict] = []
    for seg in message:
        if seg.get("type") not in ("json", "xml"):
            continue
        raw = seg.get("data", {}).get("data") or seg.get("data", {}).get("xml") or ""
        if not raw:
            continue
        title = ""
        url = ""
        try:
            obj = json.loads(raw)
            meta = (obj.get("meta") or {})
            for key in ("news", "detail_1", "music", "video"):
                block = meta.get(key)
                if isinstance(block, dict):
                    title = title or block.get("title") or block.get("desc") or ""
                    url = url or block.get("jumpUrl") or block.get("qqdocurl") or ""
        except Exception:  # noqa: BLE001
            # 不是 json，就按 xml 里找链接
            m = re.search(r"https?://[^\s\"'<>]+", raw)
            if m:
                url = m.group(0)
        if not url:
            m = re.search(r"https?://[^\s\"'<>]+", raw)
            if m:
                url = m.group(0)
        if url:
            out.append({"title": title, "url": url})
    return out


def resolve_bvid(url: str) -> str | None:
    """把 b23.tv 短链或 bilibili 链接解析成 bvid。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return m.group(1)
    if "b23.tv/" in url or "bili" in url:
        for method in ("HEAD", "GET"):
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                if method == "GET":
                    headers["Range"] = "bytes=0-0"
                req = urllib.request.Request(url, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=15) as r:
                    final = r.geturl() or ""
                m = re.search(r"(BV[0-9A-Za-z]{10})", final)
                if m:
                    return m.group(1)
            except Exception:  # noqa: BLE001
                continue
    return None


def extract_records(message) -> list[str]:
    """取出语音消息的音频 URL。"""
    if not isinstance(message, list):
        return []
    urls = []
    for seg in message:
        if seg.get("type") == "record":
            data = seg.get("data", {})
            url = data.get("url") or ""
            if url.startswith("http"):
                urls.append(url)
    return urls


def download_media(url: str, dest_dir: Path, max_bytes: int = 30 * 1024 * 1024) -> Path | None:
    """下载任意媒体，按内容挑扩展名。"""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            LOG.warning("媒体超过 %dMB，跳过", max_bytes // 1024 // 1024)
            return None
        ext = ".bin"
        for sig, e in ((b"#!AMR", ".amr"), (b"\x02#!SILK", ".silk"), (b"ID3", ".mp3"),
                       (b"RIFF", ".wav"), (b"OggS", ".ogg"), (b"ftyp", ".m4a")):
            if raw.startswith(sig) or sig in raw[:16]:
                ext = e
                break
        path = dest_dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(raw)
        return path
    except Exception:  # noqa: BLE001
        LOG.warning("下载媒体失败: %s", url, exc_info=True)
        return None


def extract_videos(message) -> list[str]:
    """取出消息里的视频 URL。"""
    if not isinstance(message, list):
        return []
    urls = []
    for seg in message:
        if seg.get("type") == "video":
            data = seg.get("data", {})
            url = data.get("url") or ""
            if url.startswith("http"):
                urls.append(url)
    return urls


WHISPER_BIN = "/opt/whisper.cpp/build/bin/whisper-cli"
# 中文用 small 量化版：比 base 准很多，比完整 small 快 3 倍
WHISPER_MODEL = "/opt/dsh-qq/models/ggml-small-q5_1.bin"
WHISPER_MAX_SECONDS = 30


def probe_duration(path: Path) -> float:
    import subprocess as sp

    try:
        out = sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


ASR_URL = "http://127.0.0.1:8790/transcribe"


def transcribe(path: Path) -> str:
    """调常驻 ASR 服务转文字（模型已常驻，不用每次加载）。"""
    try:
        payload = json.dumps({"path": str(path)}).encode()
        req = urllib.request.Request(
            ASR_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        text = (data.get("text") or "").strip()
        LOG.info("语音识别: %s 字，耗时 %ss，语言 %s", len(text), data.get("sec"), data.get("lang"))
        return text
    except Exception:  # noqa: BLE001
        LOG.warning("ASR 服务不可用，跳过语音", exc_info=True)
        return ""


def frames_for_duration(dur: float) -> int:
    """按时长决定抽几帧。"""
    if dur <= 60:
        return 6
    if dur <= 180:
        return 9
    return 12


def make_sheet(frames: list[Path], dest_dir: Path, per_row: int = 3) -> Path | None:
    """把多帧拼成一张缩略图，模型读一次就能看全片。"""
    import subprocess as sp

    if len(frames) < 2:
        return None
    cols = per_row
    rows = max(1, -(-len(frames) // cols))
    sheet = dest_dir / f"{frames[0].stem.rsplit('_', 1)[0]}_sheet.jpg"
    try:
        sp.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-pattern_type", "glob", "-i", str(frames[0].parent / f"{frames[0].stem.rsplit('_', 1)[0]}_*.jpg"),
             "-vf", f"scale=360:-2,tile={cols}x{rows}",
             "-frames:v", "1", str(sheet)],
            timeout=120, check=False,
        )
        return sheet if sheet.exists() else None
    except Exception:  # noqa: BLE001
        return None


def describe_images(paths: list[Path | str], timeout: int = 120) -> str:
    """调看图工具，把画面变成文字。主模型没有视觉，靠这个。"""
    import subprocess as sp

    files = [str(x) for x in paths if str(x).strip()][:6]
    if not files:
        return ""
    try:
        r = sp.run(["/opt/dsh-qq/describe.sh", *files],
                   capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        LOG.info("[describe] %d 张图 → %d 字", len(files), len(out))
        return out
    except Exception:  # noqa: BLE001
        LOG.warning("看图失败", exc_info=True)
        return ""


def process_video(url: str, dest_dir: Path, max_frames: int = 0) -> tuple[list[Path], str, Path | None]:
    """下载视频 → 均匀抽帧 + 拼总览图 + 转写音频。返回 (帧, 文字, 总览图)。"""
    import glob
    import subprocess as sp

    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    raw = dest_dir / f"{stem}.mp4"
    try:
        if url.startswith("file://") or url.startswith("/"):
            src = Path(url.replace("file://", ""))
            raw.write_bytes(src.read_bytes())
        else:
            size = 0
            with urllib.request.urlopen(url, timeout=60) as resp, raw.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 80 * 1024 * 1024:
                        LOG.warning("视频超过 80MB，放弃")
                        return [], "", None
                    fh.write(chunk)

        dur = probe_duration(raw)
        n = max_frames or frames_for_duration(dur)
        # 按时长均匀取样，短视频也不会挤在一起
        fps = max(0.05, n / max(dur, 1.0))
        pattern = str(dest_dir / f"{stem}_%02d.jpg")
        sp.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
             "-vf", f"fps={fps:.3f},scale=720:-2",
             "-frames:v", str(n), pattern],
            timeout=180, check=False,
        )
        frames = sorted(Path(p) for p in glob.glob(str(dest_dir / f"{stem}_*.jpg")))
        sheet = make_sheet(frames, dest_dir)

        transcript = transcribe(raw)
        LOG.info("视频: %.1fs, 抽帧 %d 张, 总览 %s, 字幕 %d 字",
                 dur, len(frames), "有" if sheet else "无", len(transcript))
        return frames, transcript, sheet
    except Exception:  # noqa: BLE001
        LOG.warning("视频处理失败: %s", url, exc_info=True)
        return [], "", None
    finally:
        try:
            raw.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def forward_ids(message) -> list[str]:
    """取出消息里「合并转发」的 id。"""
    if not isinstance(message, list):
        return []
    out = []
    for seg in message:
        if seg.get("type") == "forward":
            fid = seg.get("data", {}).get("id")
            if fid:
                out.append(str(fid))
    return out


def reply_ids(message) -> list[str]:
    """取出引用回复的原消息 id。"""
    if not isinstance(message, list):
        return []
    out = []
    for seg in message:
        if seg.get("type") == "reply":
            rid = seg.get("data", {}).get("id")
            if rid:
                out.append(str(rid))
    return out


def extract_images(message) -> list[str]:
    """取出消息里的图片 URL。"""
    if not isinstance(message, list):
        return []
    urls = []
    for seg in message:
        if seg.get("type") == "image":
            data = seg.get("data", {})
            url = data.get("url") or ""
            if url.startswith("http"):
                urls.append(url)
    return urls


def download_image(url: str, dest_dir: Path) -> Path | None:
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = resp.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            LOG.warning("图片超过 8MB，跳过")
            return None
        ext = ".jpg"
        if url.lower().endswith(".png") or b"\x89PNG" in raw[:8]:
            ext = ".png"
        path = dest_dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(raw)
        return path
    except Exception:  # noqa: BLE001
        LOG.warning("下载图片失败: %s", url, exc_info=True)
        return None


def extract_text(message) -> str:
    if isinstance(message, str):
        return re.sub(r"\[CQ:[^\]]*\]", "", message).strip()
    if not isinstance(message, list):
        return ""
    return "".join(
        seg.get("data", {}).get("text", "") for seg in message if seg.get("type") == "text"
    ).strip()


def mentioned_me(message, self_id) -> bool:
    if not isinstance(message, list):
        return False
    return any(
        seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == str(self_id)
        for seg in message
    )


MARKER = re.compile(r"\[\[(face|img|sticker):([^\]]+)\]\]")
PACE_RE = re.compile(r"\[\[pace[:=]\s*([a-zA-Z0-9.]+)\s*\]\]", re.I)
GAP_RE = re.compile(r"\[\[gap[:=]\s*([0-9.]+)\s*\]\]")
BREAK_RE = re.compile(r"\[\[\s*break\s*\]\]", re.I)

PACE_PRESET = {"fast": 0.15, "quick": 0.3, "normal": 0.9, "slow": 2.6, "later": 4.0}


def parse_pacing(text: str) -> tuple[float | None, float | None, str]:
    """模型可以用 [[pace=fast]] / [[pace=2.5]] / [[gap=0.8]] 自己决定节奏。"""
    pace = None
    gap = None

    m = PACE_RE.search(text)
    if m:
        raw = m.group(1).lower()
        if raw in PACE_PRESET:
            pace = PACE_PRESET[raw]
        else:
            try:
                pace = max(0.0, min(8.0, float(raw)))
            except ValueError:
                pace = None
        text = PACE_RE.sub("", text)

    m = GAP_RE.search(text)
    if m:
        try:
            gap = max(0.1, min(6.0, float(m.group(1))))
        except ValueError:
            gap = None
        text = GAP_RE.sub("", text)

    return pace, gap, text.strip()


def text_seg(t: str) -> dict:
    return {"type": "text", "data": {"text": t}}


RECALL_HINTS = ("上次", "之前", "那天", "还记得", "记得", "以前", "前几天", "上回",
                "我说过", "聊过", "提到过", "那个事", "那次", "前阵子", "记不记得")


def wants_recall(text: str) -> bool:
    return any(h in (text or "") for h in RECALL_HINTS)


def build_segments(text: str, base: Path) -> list[dict]:
    """把 [[face:13]] / [[img:stickers/x.png]] 转成 OneBot 消息段。"""
    segs: list[dict] = []
    pos = 0
    for m in MARKER.finditer(text):
        pre = text[pos : m.start()]
        if pre:
            segs.append(text_seg(pre))
        kind, val = m.group(1), m.group(2).strip()
        if kind == "face":
            segs.append({"type": "face", "data": {"id": str(val)}})
        else:
            p = Path(val)
            if not p.is_absolute():
                p = base / val
            try:
                if p.is_file() and p.stat().st_size <= 5 * 1024 * 1024:
                    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                    data = {"file": f"base64://{b64}"}
                    if kind == "sticker":
                        data["sub_type"] = 1  # 按表情/图片表情发出去
                    segs.append({"type": "image", "data": data})
                else:
                    LOG.warning("图片不存在或过大: %s", p)
            except Exception:  # noqa: BLE001
                LOG.warning("读取图片失败: %s", p, exc_info=True)
        pos = m.end()
    tail = text[pos:]
    if tail:
        segs.append(text_seg(tail))
    return segs or [text_seg("…")]


SELF_MARK = "## 我学到的"


def read_learned(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    idx = text.find(SELF_MARK)
    return text[idx:] if idx >= 0 else ""


def append_learned(path: Path, note: str, limit: int = 15) -> None:
    text = path.read_text(encoding="utf-8")
    note = note.strip().replace("\n", " ")[:120]
    if not note:
        return
    if SELF_MARK not in text:
        text = text.rstrip() + f"\n\n{SELF_MARK}\n\n- {note}\n"
    else:
        head, tail = text.split(SELF_MARK, 1)
        lines = [ln for ln in tail.splitlines() if ln.strip().startswith("- ")]
        lines.append(f"- {note}")
        lines = lines[-limit:]
        head = head.rstrip() + "\n\n" + SELF_MARK + "\n\n" + "\n".join(lines) + "\n"
        text = head
    path.write_text(text, encoding="utf-8")


def allowed(whitelist, value) -> bool:
    if not whitelist:
        return True
    if "*" in whitelist:
        return True
    return str(value) in {str(x) for x in whitelist}


# ───────────────────────── 桥接主体 ─────────────────────────


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ws = None
        self.seq = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.sem = asyncio.Semaphore(int(cfg["behavior"].get("max_concurrent", 2)))
        self.harness: DeepSeekHarness | None = None
        self.dsh_lock = asyncio.Lock()
        self.session_epoch: dict[str, int] = {}
        self.sessions: dict[str, object] = {}
        self.state_path = Path(cfg["dsh"]["cwd"]) / ".state" / "proactive.json"
        self.pstate: dict = self._load_state()
        self._agents_text: str | None = None

    # ---- 主动找话 ----

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.pstate, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            LOG.warning("保存主动状态失败", exc_info=True)

    def touch_user(self) -> None:
        self.pstate["last_user_ts"] = int(time.time())
        self._save_state()

    async def scheduler_loop(self):
        """它自己定的闹钟：到点就叫醒它。"""
        await asyncio.sleep(20)
        plan_task = self.cfg.get("plan_task") or {}
        plan_hour = plan_task.get("hour", 9)
        planned_day = self.pstate.get("plan_sent_day", "")

        while True:
            try:
                now = datetime.now()
                # 每天固定问一次「今天想安排点什么」
                if plan_task.get("enabled", True) and now.hour >= int(plan_hour):
                    today = now.strftime("%Y-%m-%d")
                    if planned_day != today:
                        planned_day = today
                        self.pstate["plan_sent_day"] = today
                        self._save_state()
                        await self.fire_task({"note": "给自己安排一下今天想做的事（用 sched.sh add）", "id": "plan"})

                # 到点的任务
                try:
                    import alarm

                    for item in alarm.due():
                        await self.fire_task(item)
                        alarm.complete(item)
                except Exception:  # noqa: BLE001
                    LOG.warning("闹钟检查失败", exc_info=True)
            except Exception:  # noqa: BLE001
                LOG.exception("调度循环出错")
            await asyncio.sleep(30)

    async def fire_task(self, item: dict):
        """闹钟响了：叫醒它，具体做什么由它决定。"""
        state = getattr(getattr(self.ws, "state", None), "name", "")
        if state != "OPEN":
            LOG.info("[alarm] 离线，先不触发 %s", item.get("note", "")[:30])
            return
        note = item.get("note") or ""
        qq = int((self.cfg.get("proactive") or {}).get("qq") or 0)
        if not qq:
            return
        key = f"u{qq}"
        prompt = (
            f"（系统提示）你自己定的闹钟响了，当时写的是：{note}\n"
            f"现在是 {datetime.now():%m-%d %H:%M}。\n"
            "想做什么就做：可以直接跟他说话，也可以跑工具（/opt/dsh-qq/bili_cli.sh、/opt/dsh-qq/sched.sh），"
            "也可以什么都不说、只写进日记。不想做就回 SKIP。"
        )
        try:
            sid = self.session_id(key)
            async with self.dsh_lock:
                body, _ = await asyncio.to_thread(self.run_turn, prompt, sid)
            plain = to_plain(body or "").strip()
            LOG.info("[alarm] %s → %s", note[:24], (plain[:40] or "(SKIP)"))
            if not plain or plain.upper().startswith("SKIP"):
                return
            pace, gap, plain = parse_pacing(plain)
            if pace and pace > 0:
                await asyncio.sleep(pace)
            if "[[voice" in (body or ""):
                spoken = re.sub(r"\[\[voice[^\]]*\]\]", "", plain).strip()
                if not await self.send_voice({"message_type": "private", "user_id": qq}, spoken or plain):
                    await self.send_human({"message_type": "private", "user_id": qq}, plain, gap=gap)
            else:
                await self.send_human({"message_type": "private", "user_id": qq}, plain, gap=gap)
            self.pstate["last_sent_ts"] = int(time.time())
            self._save_state()
            self.log_chat(key, "(闹钟)", plain)
        except Exception:  # noqa: BLE001
            LOG.exception("[alarm] 触发失败")

    # ---- dsh ----

    def start_harness(self):
        d = self.cfg["dsh"]
        self.harness = DeepSeekHarness(
            dsh_home=d["home"],
            cwd=d["cwd"],
            provider=d.get("provider", "deepseek-official"),
            model=d.get("model", "deepseek-v4-flash"),
            reasoning_effort=d.get("reasoning_effort"),
            max_tokens=d.get("max_tokens"),
            request_timeout_seconds=d.get("timeout_seconds", 900),
            initialize_timeout_seconds=120,
        )
        LOG.info("dsh harness 已启动")

    def _session(self, session_id: str):
        """拿到会话句柄：磁盘上已有就 resume，没有才新建。"""
        cached = self.sessions.get(session_id)
        if cached is not None:
            return cached

        assert self.harness is not None
        try:
            sess = self.harness.resume_session(session_id)
            LOG.info("恢复会话 %s", session_id)
        except Exception as e:  # noqa: BLE001
            LOG.info("会话 %s 不存在或不可恢复（%s），新建", session_id, str(e)[:80])
            sess = self.harness.start_session(session_id)
        self.sessions[session_id] = sess
        return sess

    def _agents_path(self) -> Path:
        return Path(self.cfg["dsh"]["cwd"]) / "AGENTS.md"

    def _history_dir(self) -> Path:
        return Path(self.cfg["dsh"]["cwd"]) / ".AGENTS.history"

    def snapshot_agents(self) -> None:
        """模型改过 AGENTS.md 时，把改动前的那一版压进历史，最多留 5 版。"""
        try:
            path = self._agents_path()
            try:
                cur = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                return
            if self._agents_text is None:
                self._agents_text = cur
                return
            if cur != self._agents_text:
                old = self._agents_text
                d = self._history_dir()
                d.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                (d / f"{stamp}.md").write_text(old, encoding="utf-8")
                self._agents_text = cur
                self._prune_history(5)
                LOG.info("[agents] 检测到改动，已存档旧版（%d 字）", len(old))
        except Exception:  # noqa: BLE001
            LOG.warning("快照 AGENTS.md 失败", exc_info=True)

    def _prune_history(self, keep: int = 5) -> None:
        try:
            files = sorted(self._history_dir().glob("*.md"))
            for f in files[:-keep]:
                f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _pop_history(self) -> str | None:
        """取出最近一版存档（取走即删除）。"""
        try:
            files = sorted(self._history_dir().glob("*.md"))
            if not files:
                return None
            latest = files[-1]
            text = latest.read_text(encoding="utf-8")
            latest.unlink(missing_ok=True)
            return text
        except Exception:  # noqa: BLE001
            return None

    def run_turn(self, text: str, session_id: str) -> tuple[str, str]:
        self.snapshot_agents()
        sess = self._session(session_id)
        result = sess.run(text)
        return result.final_response or "", result.finish_reason or "unknown"

    # ---- onebot ----

    async def connect(self):
        url = self.cfg["onebot"]["ws_url"]
        token = self.cfg["onebot"].get("access_token") or ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        while True:
            try:
                async with websockets.connect(url, additional_headers=headers, ping_interval=20) as ws:
                    self.ws = ws
                    LOG.info("已连接 OneBot: %s", url)
                    if self.harness is not None:
                        asyncio.create_task(self.catch_up())
                    async for raw in ws:
                        asyncio.create_task(self.on_raw(raw))
            except Exception as e:  # noqa: BLE001
                LOG.warning("连接断开 (%s)，3 秒后重连", e)
                await asyncio.sleep(3)

    async def catch_up(self):
        """重连后把掉线期间他发的消息补上看一眼。"""
        cfg = self.cfg.get("catchup") or {}
        if not cfg.get("enabled", True):
            return
        qq = int((self.cfg.get("proactive") or {}).get("qq") or 0)
        if not qq:
            return
        since = int(self.pstate.get("last_user_ts", 0))
        if not since or time.time() - since > int(cfg.get("max_hours", 6)) * 3600:
            return
        try:
            res = await self.call("get_friend_msg_history", {"user_id": qq, "count": 30}, timeout=20)
            msgs = (res.get("data") or {}).get("messages") or []
        except Exception:  # noqa: BLE001
            LOG.info("[catchup] 拿不到历史消息，跳过")
            return

        limit = int(cfg.get("max_messages", 10))
        fresh = []
        for m in msgs:
            try:
                if int(m.get("time", 0)) <= since:
                    continue
                if int((m.get("sender") or {}).get("user_id", 0)) != qq:
                    continue
            except Exception:  # noqa: BLE001
                continue
            t = extract_text(m.get("message")).strip()
            if t:
                fresh.append(t)
        if not fresh:
            return
        dropped = max(0, len(fresh) - limit)
        picked = fresh[-limit:]
        LOG.info("[catchup] 补收 %d 条（忽略 %d 条）", len(picked), dropped)

        key = f"u{qq}"
        body_text = "\n".join(f"- {t}" for t in picked)
        prompt = (
            "（系统提示）你刚掉线了一段时间，这期间他发过这些消息：\n"
            f"{body_text}\n"
            + (f"（更早还有 {dropped} 条没看）\n" if dropped else "")
            + "挑重点回他，别一条条复读，也别解释你掉线的事——除非他问了。"
        )
        try:
            sid = self.session_id(key)
            async with self.dsh_lock:
                body, _ = await asyncio.to_thread(self.run_turn, prompt, sid)
            plain = to_plain(body or "").strip()
            if plain:
                pace, gap, plain = parse_pacing(plain)
                if pace and pace > 0:
                    await asyncio.sleep(pace)
                await self.send_human({"message_type": "private", "user_id": qq}, plain, gap=gap)
                self.log_chat(key, "(补收)", plain)
        except Exception:  # noqa: BLE001
            LOG.exception("[catchup] 回复失败")

    async def call(self, action: str, params: dict, timeout: float = 20.0) -> dict:
        assert self.ws is not None
        self.seq += 1
        echo = f"dsh-{self.seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(echo, None)

    async def on_raw(self, raw: str):
        try:
            ev = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        echo = ev.get("echo")
        if echo and echo in self.pending:
            fut = self.pending[echo]
            if not fut.done():
                fut.set_result(ev)
            return
        pt = ev.get("post_type")

        # 群邀请 / 加群申请：自动同意（SnowLuma 不会把它存进待处理列表，只能实时接）
        if pt == "request" and ev.get("request_type") == "group":
            LOG.info("[group] 收到%s: %s", "邀请" if ev.get("sub_type") == "invite" else "申请",
                     json.dumps(ev, ensure_ascii=False)[:300])
            auto = (self.cfg.get("access") or {}).get("auto_accept_group", True)
            if auto:
                try:
                    res = await self.call("set_group_add_request", {
                        "flag": ev.get("flag"),
                        "sub_type": ev.get("sub_type") or "invite",
                        "approve": True,
                    })
                    LOG.info("[group] 已同意: %s", str(res)[:120])
                    self.log_chat("system", f"(收到群邀请 {ev.get('group_id')})", "我同意了")
                except Exception:  # noqa: BLE001
                    LOG.exception("[group] 同意邀请失败")
            return

        if pt == "notice":
            LOG.info("[notice] %s", json.dumps(ev, ensure_ascii=False)[:240])
            return

        if pt == "message":
            try:
                await self.on_message(ev)
            except Exception:  # noqa: BLE001
                LOG.exception("处理消息失败")

    async def send_voice(self, ev: dict, text: str) -> bool:
        """文字转语音，发成 QQ 语音条。"""
        cfg = self.cfg.get("tts") or {}
        if not cfg.get("enabled", True):
            return False
        text = text.strip()[: int(cfg.get("max_chars", 120))]
        if not text:
            return False
        try:
            import subprocess as sp

            out_dir = Path("/opt/dsh-qq/data/qq/tts")
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.mp3"
            host = out_dir / name
            cmd = [
                "/opt/dsh-qq/venv/bin/edge-tts",
                "--voice", cfg.get("voice", "zh-CN-XiaoyiNeural"),
                "--text", text, "--write-media", str(host),
            ]
            if cfg.get("rate"):
                cmd += ["--rate", str(cfg["rate"])]
            if cfg.get("pitch"):
                cmd += ["--pitch", str(cfg["pitch"])]
            sp.run(cmd, timeout=90, check=False, capture_output=True)
            if not host.exists() or host.stat().st_size < 1000:
                LOG.warning("TTS 合成失败")
                return False
            try:
                import os
                os.chown(host, 1000, 1000)
            except Exception:  # noqa: BLE001
                pass

            if ev.get("message_type") == "group":
                action, params = "send_group_msg", {"group_id": ev["group_id"]}
            else:
                action, params = "send_private_msg", {"user_id": ev["user_id"]}
            params["message"] = [{"type": "record", "data": {"file": f"/app/.config/QQ/tts/{name}"}}]
            res = await self.call(action, params, timeout=60)
            ok = res.get("status") != "failed"
            LOG.info("[voice] %s -> %s", text[:30], "ok" if ok else res)
            if ok:
                threading.Thread(target=lambda: time.sleep(600) or host.unlink(missing_ok=True), daemon=True).start()
            return ok
        except Exception:  # noqa: BLE001
            LOG.warning("发语音失败", exc_info=True)
            return False

    async def send_text(self, ev: dict, text: str):
        if ev.get("message_type") == "group":
            action, params = "send_group_msg", {"group_id": ev["group_id"]}
        else:
            action, params = "send_private_msg", {"user_id": ev["user_id"]}
        params["message"] = [{"type": "text", "data": {"text": text}}]
        res = await self.call(action, params)
        if res.get("status") == "failed":
            LOG.warning("发送失败: %s", res)
        return res.get("data", {}).get("message_id")

    async def delete_msg(self, message_id):
        if not message_id:
            return
        try:
            await self.call("delete_msg", {"message_id": message_id}, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    async def fetch_quoted(self, message_id: str) -> str:
        """取被引用那条消息的文本。"""
        try:
            mid = int(message_id) if str(message_id).isdigit() else message_id
            res = await self.call("get_msg", {"message_id": mid}, timeout=15)
            data = res.get("data") or {}
            return extract_text(data.get("message") or [])[:300]
        except Exception:  # noqa: BLE001
            LOG.warning("取引用消息失败: %s", message_id)
            return ""

    async def fetch_forward(self, fid: str, depth: int = 0) -> str:
        """把合并转发展开成「昵称: 内容」的纯文本。"""
        try:
            res = await self.call("get_forward_msg", {"message_id": fid}, timeout=30)
        except Exception:  # noqa: BLE001
            LOG.warning("取转发消息失败: %s", fid, exc_info=True)
            return ""
        data = res.get("data") or {}
        msgs = data.get("messages") or data.get("message") or []
        lines: list[str] = []
        for m in msgs[:80]:
            sender = m.get("sender") or {}
            who = sender.get("card") or sender.get("nickname") or m.get("nickname") or "?"
            content = m.get("content") or m.get("message") or []
            body = extract_text(content)
            if extract_images(content):
                body = f"{body} [图片]".strip()
            nested = forward_ids(content)
            if nested and depth < 1:
                inner = await self.fetch_forward(nested[0], depth + 1)
                if inner:
                    body = f"{body} [转发的转发：{inner[:300]}]".strip()
            if body:
                lines.append(f"{who}: {body}")
        return "\n".join(lines)

    async def send_rich(self, ev: dict, text: str):
        """支持 [[face:id]] 和 [[img:相对路径]] 的消息。"""
        if ev.get("message_type") == "group":
            action, params = "send_group_msg", {"group_id": ev["group_id"]}
        else:
            action, params = "send_private_msg", {"user_id": ev["user_id"]}
        base = Path(self.cfg["dsh"]["cwd"])
        params["message"] = build_segments(text, base)
        res = await self.call(action, params)
        if res.get("status") == "failed":
            LOG.warning("发送失败: %s", res)
        return res.get("data", {}).get("message_id")

    async def send_human(self, ev: dict, text: str, gap: float | None = None):
        """按人类节奏发出去：多气泡 + 打字停顿。gap 由模型指定时优先用。"""
        human = self.cfg["behavior"].get("humanize", True)
        if not human:
            await self.send_rich(ev, text)
            return

        max_bubbles = int(self.cfg["behavior"].get("max_bubbles", 3))
        min_chars = int(self.cfg["behavior"].get("min_bubble_chars", 45))

        auto_over = int(self.cfg["behavior"].get("auto_split_over", 0))

        if BREAK_RE.search(text):
            # 模型自己用 [[break]] 分好了条数
            parts = [p.strip() for p in BREAK_RE.split(text) if p.strip()]
            cap = int(self.cfg["behavior"].get("max_manual_bubbles", 6))
            bubbles = parts[:cap]
            LOG.info("[bubble] 模型自己切成 %d 条", len(bubbles))
        elif auto_over and len(text) > auto_over:
            # 只有特别长才自动拆，否则它写几句就是几条消息
            bubbles = split_bubbles(text, max_bubbles, min_chars)
            LOG.info("[bubble] 内容较长(%d字)，自动拆成 %d 条", len(text), len(bubbles))
        else:
            bubbles = [text]

        # QQ 单条有长度上限，只有物理上发不下时才硬切
        hard_max = int(self.cfg["behavior"].get("hard_chunk_chars", 0))
        if hard_max > 0:
            expanded: list[str] = []
            for b in bubbles:
                if len(b) <= hard_max:
                    expanded.append(b)
                else:
                    for i in range(0, len(b), hard_max):
                        expanded.append(b[i : i + hard_max])
            bubbles = expanded
        for i, part in enumerate(bubbles):
            if i:
                if gap is not None:
                    await asyncio.sleep(gap * random.uniform(0.85, 1.15))
                else:
                    # 打字时间 ≈ 基础停顿 + 字数
                    await asyncio.sleep(random.uniform(0.5, 1.2) + len(part) * 0.012)
            await self.send_rich(ev, part)

    async def on_message(self, ev: dict):
        if str(ev.get("user_id")) == str(ev.get("self_id")):
            return

        mt = ev.get("message_type")
        cfg = self.cfg["access"]
        if mt == "group":
            if not allowed(cfg.get("allow_groups"), ev.get("group_id")):
                return
            # 群里的每句话都记下来（不叫模型），这样被 @ 时它知道刚才在聊什么
            try:
                self.remember_group(ev)
            except Exception:  # noqa: BLE001
                pass
            if cfg.get("group_need_at", True) and not mentioned_me(ev.get("message"), ev.get("self_id")):
                return
        elif mt == "private":
            if not allowed(cfg.get("allow_users"), ev.get("user_id")):
                LOG.info("拒绝用户 %s", ev.get("user_id"))
                return
        else:
            return

        text = extract_text(ev.get("message"))
        image_urls = extract_images(ev.get("message"))
        video_urls = extract_videos(ev.get("message"))
        record_urls = extract_records(ev.get("message"))
        fwd_ids = forward_ids(ev.get("message"))
        rep_ids = reply_ids(ev.get("message"))

        if not text and not image_urls and not video_urls and not record_urls and not fwd_ids and not rep_ids:
            return

        # 语音条：下载后转文字
        if record_urls:
            inbox = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            heard: list[str] = []
            for url in record_urls[:2]:
                p = await asyncio.to_thread(download_media, url, inbox)
                if not p:
                    continue
                t = await asyncio.to_thread(transcribe, p)
                if t:
                    heard.append(t)
                try:
                    p.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            if heard:
                hint = "（对方发来语音，转成文字是：" + " / ".join(heard)[:800] + "）"
            else:
                hint = "（对方发来一条语音，听不清或转写失败，可以让他再发一遍）"
            text = f"{text}\n\n{hint}" if text else hint

        # 视频：抽帧成图片，让它“看”视频
        if video_urls:
            # 处理要几十秒，先说一声，免得以为没反应
            try:
                await self.send_text(ev, random.choice(["我看下", "等我瞄一眼", "稍等 我看完再说"]))
            except Exception:  # noqa: BLE001
                pass
            inbox = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            for url in video_urls[:1]:
                frames, transcript, sheet = await asyncio.to_thread(process_video, url, inbox)
                parts = []
                if sheet:
                    parts.append(f"先看这张总览图（{len(frames)} 帧拼成）：{sheet}")
                if frames:
                    parts.append("单帧：" + "、".join(str(f) for f in frames))
                if transcript:
                    parts.append(f"音频转写：{transcript[:1200]}")
                if parts:
                    visual = await asyncio.to_thread(describe_images, [sheet] if sheet else frames)
                    if visual:
                        parts.append(f"画面：{visual}")
                    hint = "（对方发来一段视频。" + "；".join(parts) + "。直接根据这些回他）"
                    text = f"{text}\n\n{hint}" if text else hint
                else:
                    text = f"{text}\n\n（对方发来一段视频，但处理失败）" if text else "（对方发来一段视频，但处理失败）"

        # 引用回复：把被引用的那句也带上
        if rep_ids:
            quoted = await self.fetch_quoted(rep_ids[0])
            if quoted:
                text = f"（他引用了这句话：{quoted}）\n{text}" if text else f"（他引用了这句话：{quoted}）"

        # 合并转发：展开成文本
        for fid in fwd_ids[:2]:
            flat = await self.fetch_forward(fid)
            if flat:
                text = (
                    f"{text}\n\n（对方转发了一段聊天记录）\n{flat}" if text else f"（对方转发了一段聊天记录）\n{flat}"
                )
                LOG.info("展开转发 %s，%d 字", fid, len(flat))

        if image_urls:
            inbox = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            saved = []
            for url in image_urls[:3]:
                p = await asyncio.to_thread(download_image, url, inbox)
                if p:
                    saved.append(p)
            if saved:
                visual = await asyncio.to_thread(describe_images, saved)
                hint = "（对方发来图片" + (f"，画面内容是：{visual}" if visual else "，但看不清") + "）"
                text = f"{text}\n\n{hint}" if text else hint
            LOG.info("收到 %d 张图片，成功下载 %d 张", len(image_urls), len(saved))

        key = f"g{ev['group_id']}-{ev['user_id']}" if mt == "group" else f"u{ev['user_id']}"
        LOG.info("[in] %s: %s", key, text[:120].replace("\n", " "))
        self.touch_user()

        # 链接来源：卡片（json/xml）+ 纯文字里的 URL，两种都认
        shares = extract_share_links(ev.get("message"))
        for u in re.findall(r"https?://[^\s，。、）)】]+", text):
            shares.append({"title": "", "url": u})

        for s in shares:
            bvid = resolve_bvid(s["url"])
            if bvid:
                LOG.info("[bili] 链接识别 %s %s", bvid, s.get("title", "")[:30])
                text = await self.attach_bili(bvid, text)
                break
            if "douyin.com" in s["url"]:
                LOG.info("[douyin] 链接识别 %s", s["url"][:80])
                text = await self.attach_douyin(s["url"], text)
                break
            if s.get("title"):
                text = f"{text}\n\n（对方分享了一个链接：{s['title']}）"

        lowered = text.strip().lower()
        if lowered in ("/new", "／new"):
            self.session_epoch[key] = self.session_epoch.get(key, 0) + 1
            await self.send_text(ev, "行，重新开始。")
            return
        if lowered in ("/help", "／help"):
            await self.send_text(ev, HELP_TEXT)
            return
        if lowered in ("/status", "／status"):
            await self.send_text(ev, f"当前会话 {self.session_id(key)}")
            return

        if lowered in ("/tasks", "／tasks"):
            try:
                import alarm

                items = alarm.load()
            except Exception:  # noqa: BLE001
                items = []
            if not items:
                await self.send_text(ev, "它还没给自己排任务")
                return
            lines = []
            for i in items[:15]:
                flag = " 每天" if i.get("repeat") == "daily" else ""
                lines.append(f"{alarm.fmt(i['due'])}{flag}  {i['note'][:40]}")
            await self.send_text(ev, "\n".join(lines))
            return

        if lowered.startswith(("/bili", "／bili")):
            # 不预告，静静刷完直接说话；带关键词就按关键词搜
            kw = text.strip()[5:].strip()
            asyncio.create_task(self.watch_bili(ev, key, keyword=kw or None))
            return

        agents = Path(self.cfg["dsh"]["cwd"]) / "AGENTS.md"
        golden = Path(self.cfg["dsh"]["cwd"]) / ".AGENTS.golden.md"

        if lowered.startswith(("/learn", "／learn")):
            note = text.split(" ", 1)[1] if " " in text else ""
            if not note:
                await self.send_text(ev, "要说学啥啊，比如 /learn 别用省略号")
                return
            append_learned(agents, note)
            await self.send_text(ev, "记下了")
            return

        if lowered in ("/persona", "／persona"):
            learned = read_learned(agents).strip()
            await self.send_text(ev, learned[:600] if learned else "还没学到啥")
            return

        memory_dir = Path(self.cfg["dsh"]["cwd"]) / "memory"
        facts = memory_dir / "facts.md"

        if lowered.startswith(("/remember", "／remember")):
            note = text.split(" ", 1)[1] if " " in text else ""
            if not note:
                await self.send_text(ev, "要记啥？/remember 他在广州做后端")
                return
            memory_dir.mkdir(parents=True, exist_ok=True)
            with facts.open("a", encoding="utf-8") as fh:
                fh.write(f"- {time.strftime('%Y-%m-%d')} {note.strip()[:120]}\n")
            await self.send_text(ev, "记住了")
            return

        if lowered in ("/memory", "／memory"):
            try:
                body = facts.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                body = ""
            body = body or "还什么都没记"
            await self.send_text(ev, body[-700:])
            return

        if lowered in ("/reflect", "／reflect"):
            await self.send_text(ev, "我翻翻最近的聊天…")
            await self.reflect(ev)
            return

        if lowered in ("/forget", "／forget"):
            # 一次退一级，退到底才恢复出厂
            restored = self._pop_history()
            if restored is not None:
                agents.write_text(restored, encoding="utf-8")
                self._agents_text = agents.read_text(encoding="utf-8")
                left = len(list(self._history_dir().glob("*.md")))
                await self.send_text(ev, f"退了一步（还能退 {left} 步）")
            elif golden.exists():
                agents.write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
                self._agents_text = agents.read_text(encoding="utf-8")
                await self.send_text(ev, "没有可撤销的，恢复出厂了")
            else:
                await self.send_text(ev, "没有备份，清不了")
            return

        if lowered in ("/reset", "／reset"):
            if golden.exists():
                agents.write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
                self._agents_text = agents.read_text(encoding="utf-8")
                self._prev_path().unlink(missing_ok=True)
                await self.send_text(ev, "恢复出厂了")
            else:
                await self.send_text(ev, "没有出厂备份")
            return

        # 群里被 @ 时，附上刚才群里在聊什么
        if mt == "group":
            try:
                ctx = self.recent_group(int(ev["group_id"]), 15)
                if ctx:
                    text = f"{text}\n\n（群里刚才在聊：\n" + "\n".join(f"- {c}" for c in ctx) + "）"
            except Exception:  # noqa: BLE001
                pass

        # 他说到“上次/之前/还记得”这类词时，去索引里翻旧对话
        if wants_recall(text):
            try:
                import chatmem

                hits = await asyncio.to_thread(chatmem.search, text, 3, 2)
                if hits:
                    lines = "\n".join(f"- {h['line']}" for h in hits)
                    text = f"{text}\n\n（你想起以前聊过：\n{lines}\n不确定就问他，别硬编）"
                    LOG.info("[mem] 命中 %d 条旧对话", len(hits))
            except Exception:  # noqa: BLE001
                LOG.debug("翻旧对话失败", exc_info=True)

        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self.sem:
                await self.handle_turn(ev, text, key)

    def remember_group(self, ev: dict, keep: int = 200) -> None:
        """把群消息滚进一个环形日志，供被 @ 时回忆。"""
        text = extract_text(ev.get("message")).strip()
        if not text:
            return
        gid = ev.get("group_id")
        sender = (ev.get("sender") or {}).get("card") or (ev.get("sender") or {}).get("nickname") or ev.get("user_id")
        d = Path(self.cfg["dsh"]["cwd"]) / ".group"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{gid}.jsonl"
        try:
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": int(time.time()), "who": str(sender)[:20],
                                     "text": text[:200]}, ensure_ascii=False) + "\n")
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > keep:
                f.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def recent_group(self, gid: int, n: int = 12) -> list[str]:
        f = Path(self.cfg["dsh"]["cwd"]) / ".group" / f"{gid}.jsonl"
        if not f.exists():
            return []
        out = []
        try:
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                out.append(f"{d.get('who')}: {d.get('text')}")
        except Exception:  # noqa: BLE001
            return []
        return out

    def log_chat(self, key: str, user: str, bot: str):
        """把对话存下来，供每日自省用。"""
        try:
            d = Path(self.cfg["dsh"]["cwd"]) / ".chatlog"
            d.mkdir(parents=True, exist_ok=True)
            f = d / (time.strftime("%Y-%m-%d") + ".jsonl")
            with f.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"ts": int(time.time()), "key": key, "user": user, "bot": bot},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            LOG.warning("写聊天记录失败", exc_info=True)
        # 顺手写一行索引，供以后按关键词找回来
        try:
            import chatmem

            chatmem.append(user, bot, key, int(time.time()))
        except Exception:  # noqa: BLE001
            pass

    async def watch_bili(self, ev: dict, key: str, bvid: str | None = None, keyword: str | None = None):
        """刷 B 站视频，看完主动聊两句。可以指定 bvid，或按关键词搜，或随机分区刷。"""
        try:
            import bili

            dest = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            watched = set(self.pstate.get("bili_watched", []))

            if bvid:
                meta, path = await asyncio.to_thread(bili.fetch_bvid, bvid, dest)
                batch = [(meta, path)]
            elif keyword:
                got = await asyncio.to_thread(bili.fetch_by_keyword, keyword, dest, watched)
                batch = [got] if got else []
                if not batch:
                    await self.send_text(ev, f"「{keyword}」没搜到合适的视频")
                    return
            else:
                n = await self.pick_bili_count(key)
                LOG.info("[bili] 它想看 %d 个", n)
                batch = await asyncio.to_thread(bili.fetch_many, dest, n, watched)
            if not batch:
                await self.send_text(ev, "没刷到合适的，等会儿再看")
                return

            entries = []
            for meta, path in batch:
                LOG.info("[bili] %s %s (%ss) 分区=%s", meta["bvid"], meta["title"][:24],
                         meta["duration"], meta.get("zone") or meta["tname"])
                frames, transcript, sheet = await asyncio.to_thread(process_video, str(path), dest)
                entries.append((meta, frames, transcript, sheet))
                watched.add(meta["bvid"])
                try:
                    path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

            self.pstate["bili_watched"] = list(watched)[-60:]
            self._save_state()

            if len(entries) == 1:
                parts = ["你刚在 B 站刷了个视频，现在想跟他聊聊这个。"]
            else:
                parts = [f"你刚在 B 站刷了 {len(entries)} 个视频，现在想跟他聊其中一个（挑最有意思的那个）。"]
            for i, (meta, frames, transcript, sheet) in enumerate(entries, 1):
                parts.append(f"\n【第 {i} 个】{meta['title']}")
                parts.append(f"UP主 {meta['up']}｜分区 {meta.get('zone') or meta['tname']}｜{meta['duration']} 秒")
                if meta["desc"]:
                    parts.append(f"简介：{meta['desc'][:200]}")
                visual = await asyncio.to_thread(describe_images, [sheet] if sheet else frames)
                if visual:
                    parts.append("画面：" + visual)
                if transcript:
                    parts.append("台词：" + transcript[:800])
                try:
                    hot = await asyncio.to_thread(bili.comments, meta["bvid"], 8)
                    if hot:
                        top = "；".join(f"{c['user']}（{c['like']}赞）{c['text'][:40]}" for c in hot[:6])
                        parts.append("热评：" + top)
                except Exception:  # noqa: BLE001
                    pass
            parts.append(
                "\n先跑 /opt/dsh-qq/describe.sh 看画面、读台词，挑一个你觉得最有意思的，自然地跟他提一嘴——看了什么、什么感觉。"
                "想多说就多说，别念简介、别像推荐语就行。"
                "看到评论区有想回的，可以自己发：/opt/dsh-qq/bili_cli.sh post BV号 \"你想说的话\"。"
            )

            sid = self.session_id(key)
            async with self.dsh_lock:
                body, reason = await asyncio.to_thread(self.run_turn, "（系统提示）" + "\n".join(parts), sid)

            plain = to_plain(body or "").strip()
            if plain:
                # 主动消息也要解析标记，否则 [[pace=...]] 会当正文发出去
                pace, gap, plain = parse_pacing(plain)
                if pace and pace > 0:
                    await asyncio.sleep(pace)
                await self.send_human(ev, plain, gap=gap)
                self.log_chat(key, "(主动)", plain)
        except Exception as e:  # noqa: BLE001
            LOG.exception("刷视频失败")
            try:
                await self.send_text(ev, f"刷视频出问题了：{str(e)[:120]}")
            except Exception:  # noqa: BLE001
                pass

    async def pick_bili_count(self, key: str) -> int:
        """让它自己决定这次刷几个（1–4）。"""
        try:
            prompt = (
                "（系统提示）你准备刷会儿 B 站。"
                "这次想看几个？在 1 到 4 之间选一个数，看心情就行。只输出那个数字，不要解释。"
            )
            sid = self.session_id(key)
            async with self.dsh_lock:
                body, _ = await asyncio.to_thread(self.run_turn, prompt, sid)
            text = body or ""
            m = re.search(r"[1-4]", text)
            if m:
                return int(m.group(0))
            cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4}
            for ch, n in cn.items():
                if ch in text:
                    return n
            return 2
        except Exception:  # noqa: BLE001
            LOG.warning("挑数量失败", exc_info=True)
            return 2

    async def pick_bili_keyword(self, key: str) -> str | None:
        """让它自己决定想刷什么。"""
        try:
            prompt = (
                "（系统提示）你现在想刷会儿 B 站。"
                "用 2–8 个字说说你想看什么，可以是话题、游戏、up主类型。"
                "只输出这几个字，不要标点、不要解释。"
            )
            sid = self.session_id(key)
            async with self.dsh_lock:
                body, _ = await asyncio.to_thread(self.run_turn, prompt, sid)
            kw = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", (body or "").strip())[:10]
            return kw or None
        except Exception:  # noqa: BLE001
            LOG.warning("挑关键词失败", exc_info=True)
            return None

    async def attach_bili(self, bvid: str, text: str) -> str:
        """把 B 站视频的画面和台词接在这条消息后面。"""
        try:
            import bili

            dest = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            meta, path = await asyncio.to_thread(bili.fetch_bvid, bvid, dest)
            frames, transcript, sheet = await asyncio.to_thread(process_video, str(path), dest)
            parts = [
                f"（他发来一个B站视频：{meta['title']}｜UP主 {meta['up']}｜{meta['duration']} 秒"
                + (f"｜简介 {meta['desc'][:200]}" if meta["desc"] else "")
                + "）"
            ]
            visual = await asyncio.to_thread(describe_images, [sheet] if sheet else frames)
            if visual:
                parts.append("画面：" + visual)
            if transcript:
                parts.append("台词转写：" + transcript[:1200])
            try:
                hot = await asyncio.to_thread(bili.comments, bvid, 8)
                if hot:
                    top = "；".join(f"{c['user']}（{c['like']}赞）{c['text'][:40]}" for c in hot[:6])
                    parts.append("热评：" + top)
            except Exception:  # noqa: BLE001
                pass
            parts.append("看完再回他这句话，别装没看见视频。")
            LOG.info("[bili] 已附上 %s（帧 %d，台词 %d 字）", bvid, len(frames), len(transcript))
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return f"{text}\n\n" + "\n".join(parts)
        except Exception as e:  # noqa: BLE001
            LOG.exception("附 B 站视频失败")
            return f"{text}\n\n（他发来一个B站视频 BV{bvid}，但下载失败：{str(e)[:100]}）"

    async def attach_douyin(self, url: str, text: str) -> str:
        try:
            import douyin

            dest = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            got = await asyncio.to_thread(douyin.download, url, dest)
            if not got:
                return f"{text}\n\n（他发来一个抖音视频，但下载失败）"
            meta, path = got
            frames, transcript, sheet = await asyncio.to_thread(process_video, str(path), dest)
            parts = [f"（他发来一个抖音视频：{meta['title']}｜作者 {meta['up'] or '未知'}）"]
            if sheet:
                parts.append(f"总览图（{len(frames)} 帧拼成，先看这张）：{sheet}")
            if frames:
                parts.append("单帧：" + "、".join(str(f) for f in frames))
            if transcript:
                parts.append("语音转写：" + transcript[:1200])
            parts.append("看完再回他这句话。")
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return f"{text}\n\n" + "\n".join(parts)
        except Exception as e:  # noqa: BLE001
            LOG.exception("附抖音视频失败")
            return f"{text}\n\n（他发来一个抖音视频，但下载失败：{str(e)[:100]}）"

    async def watch_douyin(self, ev: dict, key: str, url: str):
        """看一个抖音视频，看完聊两句。"""
        try:
            import douyin

            dest = Path(self.cfg["dsh"]["cwd"]) / ".inbox"
            got = await asyncio.to_thread(douyin.download, url, dest)
            if not got:
                await self.send_text(ev, "这个抖音视频我下不下来，可能得登录或者已经删了")
                return
            meta, path = got
            LOG.info("[douyin] %s (%ss) %s", meta["title"][:30], meta["duration"], meta["quality"])

            frames, transcript, sheet = await asyncio.to_thread(process_video, str(path), dest)

            parts = [
                "你刚刷到一个抖音视频，现在想跟他聊聊这个。",
                f"标题：{meta['title']}",
                f"作者：{meta['up'] or '不知道'}｜时长：{meta['duration']} 秒",
            ]
            if meta["desc"]:
                parts.append(f"文案：{meta['desc']}")
            if sheet:
                parts.append(f"总览图（{len(frames)} 帧拼成，先看这张）：{sheet}")
            if frames:
                parts.append("单帧：" + "、".join(str(f) for f in frames))
            if transcript:
                parts.append("语音转写：" + transcript[:1000])
            parts.append("先看图和台词，然后自然地跟他提一嘴——看了什么、什么感觉。想多说就多说，别像推荐语就行。")

            sid = self.session_id(key)
            async with self.dsh_lock:
                body, _ = await asyncio.to_thread(self.run_turn, "（系统提示）" + "\n".join(parts), sid)

            plain = to_plain(body or "").strip()
            if plain:
                pace, gap, plain = parse_pacing(plain)
                if pace and pace > 0:
                    await asyncio.sleep(pace)
                await self.send_human(ev, plain, gap=gap)
                self.log_chat(key, "(主动)", plain)
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            LOG.exception("看抖音失败")
            try:
                await self.send_text(ev, f"抖音这个没看成：{str(e)[:120]}")
            except Exception:  # noqa: BLE001
                pass

    async def reflect(self, ev: dict):
        script = BASE / "reflect.py"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(BASE),
            )
            LOG.info("自省完成: %s", (proc.stdout or "").strip()[-300:])
            if proc.returncode != 0:
                LOG.warning("自省失败: %s", (proc.stderr or "")[-300:])
        except Exception as e:  # noqa: BLE001
            LOG.warning("自省异常: %s", e)

        agents = Path(self.cfg["dsh"]["cwd"]) / "AGENTS.md"
        learned = read_learned(agents).strip()
        await self.send_text(ev, learned[:800] if learned else "没想出啥新的")

    def session_id(self, key: str) -> str:
        epoch = self.session_epoch.get(key, 0)
        return f"qq-{key}" if epoch == 0 else f"qq-{key}-e{epoch}"

    async def handle_turn(self, ev: dict, text: str, key: str):
        sid = self.session_id(key)
        ack_id = None
        ack_task = None
        ack_delay = self.cfg["behavior"].get("ack_delay_ms", 6000) / 1000
        min_turn = self.cfg["behavior"].get("min_turn_seconds", 1.3)

        async def ack():
            nonlocal ack_id
            await asyncio.sleep(ack_delay)
            try:
                ack_id = await self.send_text(ev, random.choice(["我想想…", "嗯…", "等我一下"]))
            except Exception:  # noqa: BLE001
                pass

        # ack_delay_ms=0 时完全不发「我想想」，回多快、发什么全由它自己决定
        ack_task = asyncio.create_task(ack()) if ack_delay > 0 else None
        started = time.time()
        try:
            async with self.dsh_lock:
                try:
                    body, reason = await asyncio.to_thread(self.run_turn, text, sid)
                except Exception as e:  # noqa: BLE001
                    # 会话 id 冲突（磁盘上残留）时换一个 epoch 重来一次
                    if "already exists" in str(e):
                        LOG.warning("会话 %s 已存在，换新会话重试", sid)
                        self.session_epoch[key] = self.session_epoch.get(key, 0) + 1
                        sid = self.session_id(key)
                        body, reason = await asyncio.to_thread(self.run_turn, text, sid)
                    else:
                        raise
            elapsed = time.time() - started
            LOG.info("[out] %s %.1fs %d 字 (%s)", key, elapsed, len(body), reason)

            if reason not in ("completed", "max-tokens") and not body:
                body = f"（这轮没跑完：{reason}）"

            # 模型可以用 [[pace=fast|slow|2.5]] / [[gap=0.8]] 自己决定节奏
            pace, gap, stripped = parse_pacing(body or "")
            plain = to_plain(stripped)

            if pace is not None:
                delay = pace
                LOG.info("[pace] 模型自选 %.2fs", delay)
            elif min_turn > 0:
                delay = max(0.0, min_turn - elapsed) + random.uniform(0.0, 0.8)
            else:
                delay = 0.0  # 不设下限，模型自己决定快慢

            if delay > 0:
                await asyncio.sleep(delay)

            if ack_task: ack_task.cancel()
            await self.delete_msg(ack_id)

            # [[voice]] → 合成语音条发出去
            if "[[voice" in (body or ""):
                spoken = re.sub(r"\[\[voice[^\]]*\]\]", "", plain).strip()
                sent = await self.send_voice(ev, spoken or plain)
                if not sent:
                    await self.send_human(ev, plain or "……", gap=gap)
            else:
                await self.send_human(ev, plain or "……", gap=gap)
            self.log_chat(key, text, plain)
        except Exception as e:  # noqa: BLE001
            if ack_task: ack_task.cancel()
            await self.delete_msg(ack_id)
            LOG.exception("运行失败")
            await self.send_text(ev, f"出错了：{str(e)[:200]}")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    bridge = Bridge(cfg)
    bridge.start_harness()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(bridge.connect())
    scheduler = asyncio.create_task(bridge.scheduler_loop())
    await stop.wait()
    task.cancel()
    scheduler.cancel()
    if bridge.harness:
        bridge.harness.close()
    LOG.info("已退出")


if __name__ == "__main__":
    asyncio.run(main())
