#!/usr/bin/env python3
"""看图工具：用支持视觉的模型描述图片，把结果当文字返回。

用法:
    describe.py 图片路径 [更多路径...]

给「会思考但没有视觉」的主模型用：它看不到图，但能调这个命令拿到文字描述。
"""
import sys
import time

sys.path.insert(0, "/opt/dsh-qq/bridge")
from deepseek_harness import DeepSeekHarness

PROMPT = (
    "用 read_image 逐张看下面这些图片，然后客观描述画面：有谁、在哪、在做什么、"
    "有没有文字（把文字念出来）、整体是什么场景或氛围。"
    "150 字以内，只描述看到的东西，不要评价、不要猜测。\n\n图片：\n"
)


def main() -> int:
    paths = [p for p in sys.argv[1:] if p.strip()]
    if not paths:
        print("用法: describe.py 图片路径 [...]")
        return 1

    h = DeepSeekHarness(
        dsh_home="/opt/dsh-qq/home",
        cwd="/opt/dsh-qq/workspace",
        provider="deepseek-official",
        model="deepseek-flash",
        request_timeout_seconds=180,
    )
    sid = f"desc-{int(time.time() * 1000)}"
    try:
        h.start_session(sid)
        prompt = PROMPT + "\n".join(paths[:6])
        r = h.run(prompt, session_id=sid)
        print((getattr(r, "final_response", "") or "").strip())
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"（看图失败：{str(e).splitlines()[0][:120]}）")
        return 1
    finally:
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
