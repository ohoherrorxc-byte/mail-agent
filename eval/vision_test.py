"""
vision_test.py — 把本地图片喂给 Azure OpenAI gpt-5.4 vision，
让 LLM 描述图片内容。验证 vision 是否能通。

不连 mailbox，不动任何邮件。只读本地文件 + 调 LLM。
（遵守 CLAUDE.md 硬约束：不 send / 不 delete / 不动原邮件）

跑法：
  python3 eval/vision_test.py --image /tmp/vision_test/IMG_6079.png
  python3 eval/vision_test.py --image ./foo.png --prompt "图里有几个红圈？"
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import _env_loader
_env_loader.load_env(verbose=False)

from openai import AzureOpenAI  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True, help="本地图片路径")
    p.add_argument(
        "--prompt",
        default=(
            "请用中文详细描述这张手机截图：\n"
            "1) 这是哪个 app 的哪个页面（顶部标题 / tab 名）？\n"
            "2) 显示了哪些车型 / 车牌 / 车辆状态信息？\n"
            "3) 有没有错误 / 报错信息？如果有，**逐字引用完整错误原文**（含括号里错误码）\n"
            "4) 截图分辨率多大？可见哪些按钮 / 图标？\n"
            "5) 这张图看起来是 iSMART app 报错界面吗？为什么？"
        ),
    )
    args = p.parse_args()

    img_path = Path(args.image)
    if not img_path.exists():
        print(f"[ERR] 图片不存在: {img_path}", file=sys.stderr)
        return 2

    raw = img_path.read_bytes()
    ctype = mimetypes.guess_type(img_path.name)[0] or "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{ctype};base64,{b64}"

    print(f"图片: {img_path}")
    print(f"  字节数:   {len(raw):,} ({len(raw)/1024:.1f} KB)")
    print(f"  base64:   {len(b64):,} 字符 (~{len(b64)/1024:.1f} KB)")
    print(f"  data URL: {len(data_url):,} 字符")
    print(f"  MIME:     {ctype}")
    print()

    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = os.environ.get(
        "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
    )

    client = AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint,
        timeout=120,
    )

    print(f"endpoint:    {endpoint}")
    print(f"deployment:  {deployment}")
    print(f"api_version: {api_version}")
    print()
    print("─── 调 gpt-5.4 vision (reasoning_effort=low, max_tokens=800) ───")
    resp = client.chat.completions.create(
        model=deployment,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": args.prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        reasoning_effort="low",
        max_completion_tokens=8192,
    )

    text = (resp.choices[0].message.content or "").strip()
    finish = getattr(resp.choices[0], "finish_reason", None)

    print()
    print("=" * 60)
    print("LLM 描述")
    print("=" * 60)
    print(text)
    print("=" * 60)
    print()

    u = getattr(resp, "usage", None)
    print(f"finish_reason: {finish}")
    if u:
        print(
            f"usage.prompt_tokens:       {u.prompt_tokens:,}"
        )
        print(
            f"usage.completion_tokens:   {u.completion_tokens:,}"
        )
        ptd = getattr(u, "prompt_tokens_details", None)
        if ptd:
            print(
                f"usage.cached_tokens:       {getattr(ptd, 'cached_tokens', 0):,}"
            )
        ctd = getattr(u, "completion_tokens_details", None)
        if ctd:
            print(
                f"usage.reasoning_tokens:    {getattr(ctd, 'reasoning_tokens', 0):,}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())