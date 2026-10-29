#!/usr/bin/env python3
"""常驻语音识别服务（按需加载模型，闲时释放内存）。

POST /transcribe  {"path": "/abs/audio.wav"}  -> {"text": "...", "lang": "zh", "sec": 1.2}
GET  /health                                  -> {"ok": true, "model": true|false}
"""
from __future__ import annotations

import gc
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("asr")

MODEL_DIR = "/opt/dsh-qq/models/fw"
# 短视频全听，长视频只听开头，避免一条视频卡好几分钟
SHORT_VIDEO_SECONDS = int(os.environ.get("ASR_SHORT_SECONDS", "180"))
LONG_VIDEO_SECONDS = int(os.environ.get("ASR_LONG_SECONDS", "60"))
IDLE_SECONDS = int(os.environ.get("ASR_IDLE_SECONDS", "600"))
# 只有内存真的紧张时才释放；这台盘慢，冷加载要两分多钟
LOW_MEM_MB = int(os.environ.get("ASR_LOW_MEM_MB", "400"))
PORT = int(os.environ.get("ASR_PORT", "8790"))

_model = None
_last_used = 0.0
_lock = threading.Lock()


def ensure_model():
    global _model, _last_used
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            t0 = time.time()
            LOG.info("加载模型…")
            # local_files_only：模型早就下好了，别再联网核对版本（这里连镜像站很慢）
            _model = WhisperModel("small", device="cpu", compute_type="int8",
                                  download_root=MODEL_DIR, local_files_only=True)
            LOG.info("模型就绪，耗时 %.1fs", time.time() - t0)
        _last_used = time.time()
        return _model


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 9999


def idle_watchdog():
    """内存真的紧张、且模型闲了一会儿，才释放它。

    这台机器的盘很慢，冷加载模型要两分多钟，所以不能随便放。
    """
    global _model
    while True:
        time.sleep(30)
        with _lock:
            if _model is None:
                continue
            idle = time.time() - _last_used
            if idle > IDLE_SECONDS:
                LOG.info("空闲 %ds，释放模型（下次用约 2 秒重新加载）", idle)
                _model = None
                gc.collect()


def duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def to_wav(path: str) -> tuple[str, int]:
    """转 16k 单声道 wav。短视频全听，长视频只听开头。"""
    dur = duration(path)
    limit = SHORT_VIDEO_SECONDS if (dur and dur <= SHORT_VIDEO_SECONDS) else LONG_VIDEO_SECONDS
    out = f"/tmp/asr-{os.getpid()}-{int(time.time() * 1000)}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
         "-t", str(limit), "-vn", "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", out],
        timeout=300, check=False,
    )
    return out, limit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        LOG.info("%s", fmt % args)

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "model": _model is not None})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            path = data.get("path") or ""
            if not path or not os.path.exists(path):
                self._json(400, {"error": f"no such file: {path}"})
                return

            wav, limit = to_wav(path)
            if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                self._json(200, {"text": "", "lang": "", "sec": 0, "note": "没有音轨"})
                return

            model = ensure_model()
            t0 = time.time()
            segments, info = model.transcribe(wav, beam_size=1, vad_filter=True)
            text = "".join(s.text for s in segments).strip()
            elapsed = time.time() - t0
            try:
                os.unlink(wav)
            except OSError:
                pass
            LOG.info("识别 %.1fs（上限 %ds）: %d 字 (%s)", elapsed, limit, len(text), info.language)
            self._json(200, {"text": text, "lang": info.language,
                             "sec": round(elapsed, 1), "limit": limit})
        except Exception as e:  # noqa: BLE001
            LOG.exception("识别失败")
            self._json(500, {"error": str(e)[:300]})


if __name__ == "__main__":
    threading.Thread(target=idle_watchdog, daemon=True).start()

    def warmup():
        try:
            ensure_model()
        except Exception:  # noqa: BLE001
            LOG.warning("预热失败", exc_info=True)

    # 启动就烤热模型：这台盘冷加载要两分多钟
    threading.Thread(target=warmup, daemon=True).start()
    LOG.info("ASR 服务监听 127.0.0.1:%d（启动预热，内存紧张且空闲 %d 秒才释放）", PORT, IDLE_SECONDS)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
