"""独立 GPT-SoVITS TTS 调用脚本(不依赖 mohobot, 仅需 httpx)。

调用 GPT-SoVITS api_v2 的 /tts 接口, 把文本合成为音频文件。

用法:
    python scripts/tts_standalone.py "你好呀" -o out.wav
    python scripts/tts_standalone.py "你好呀" --url http://127.0.0.1:9880 \
        --ref "D:/GPT-SoVITS/refs/voice.wav" --prompt-text "参考音频说的原文"
    python scripts/tts_standalone.py            # 无文本参数进入交互模式, 逐行合成

常用参数(都有默认值, 也可用环境变量 GSV_TTS_URL / GSV_TTS_REF 覆盖):
    --url          GSV api_v2 地址(默认 http://127.0.0.1:9880)
    -o/--out       输出音频文件(默认 ./tts_out.wav)
    --ref          参考音频路径(GSV 服务器本机路径)
    --prompt-text  参考音频对应的原文
    --text-lang    合成文本语种(默认 zh)
    --media-type   音频格式 wav/ogg/aac(默认 wav)
    --speed        语速倍率(默认 1.0)
    --timeout      请求超时秒数(默认 60)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import httpx
except ImportError:
    print("缺少依赖: pip install httpx", file=sys.stderr)
    sys.exit(1)


def build_payload(args: argparse.Namespace, text: str) -> dict:
    return {
        "text": text,
        "text_lang": args.text_lang,
        "ref_audio_path": args.ref,
        "prompt_text": args.prompt_text,
        "prompt_lang": args.text_lang,
        "media_type": args.media_type,
        "speed_factor": args.speed,
        "text_split_method": "cut5",
        "streaming_mode": False,
    }


def synthesize(args: argparse.Namespace, text: str) -> bytes:
    """调用 /tts 合成, 返回音频字节; 失败时抛 RuntimeError。"""
    url = args.url.rstrip("/") + "/tts"
    with httpx.Client(timeout=args.timeout) as client:
        resp = client.post(url, json=build_payload(args, text))
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    audio = resp.content
    if not audio:
        raise RuntimeError("服务返回空音频")
    return audio


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT-SoVITS 独立 TTS 调用")
    parser.add_argument("text", nargs="*", help="要合成的文本(缺省进入交互模式)")
    parser.add_argument("--url", default=os.environ.get("GSV_TTS_URL", "http://127.0.0.1:9880"))
    parser.add_argument("-o", "--out", default="tts_out.wav")
    parser.add_argument("--ref", default=os.environ.get("GSV_TTS_REF", ""),
                        help="参考音频路径(GSV 服务器本机路径)")
    parser.add_argument("--prompt-text", default="", help="参考音频原文")
    parser.add_argument("--text-lang", default="zh")
    parser.add_argument("--media-type", default="wav", choices=["wav", "ogg", "aac"])
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if not args.ref:
        print("提示: 未指定 --ref 参考音频(GSV 零样本克隆需要), 服务端可能报错\n")

    texts: list[str] = [" ".join(args.text).strip()] if args.text else []
    interactive = not texts
    out_path = args.out

    while True:
        if interactive:
            try:
                line = input("输入文本(空行退出) > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            text = line
        else:
            text = texts[0]

        started = time.time()
        try:
            audio = synthesize(args, text)
        except RuntimeError as e:
            print(f"❌ 合成失败: {e}", file=sys.stderr)
            if not interactive:
                sys.exit(1)
            continue
        except Exception as e:
            print(f"❌ 请求异常: {e}", file=sys.stderr)
            if not interactive:
                sys.exit(1)
            continue

        # 交互模式按序号命名, 非交互用 -o 指定的文件名
        save_path = out_path
        if interactive:
            root, ext = os.path.splitext(out_path)
            save_path = f"{root}_{int(time.time())}{ext or '.wav'}"
        with open(save_path, "wb") as f:
            f.write(audio)
        print(f"✅ {len(text)} 字 → {save_path} ({len(audio) / 1024:.0f} KB, "
              f"耗时 {time.time() - started:.1f}s)")

        if not interactive:
            break


if __name__ == "__main__":
    main()
