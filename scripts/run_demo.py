#!/usr/bin/env python3
"""
run_demo.py — M1 PoC 演示入口。

读入一封英文售后邮件，串 EmailAgent，输出：

  1) Triage / Retrieval / Draft / Compliance 四段 JSON
  2) 草稿邮件正文（Markdown）——这是要送人工工作台审核的东西
  3) 审计摘要（每个 LLM 调用的 token 用量 + cache hit 率）

不发送邮件——所有外发动作必须经人工在工作台里点确认。

用法：
    export $(grep -v '^#' ../.env | xargs)
    python run_demo.py                   # 默认读 ./sample_email.txt
    python run_demo.py /path/to/email    # 读任意邮件正文
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from textwrap import indent

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先 .env（已 gitignore），再 .env.example 作为后备（开发期会塞真 key）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")            # 真生产配置
    load_dotenv(ROOT / ".env.example",    # 开发期 fallback，**不会覆盖**已设的 env
                override=False)
except ImportError:
    pass                                   # 没装 dotenv 时继续，env 由 shell 提供

from agent import AzureLLMClient, EmailAgent            # noqa: E402
from agent.email_agent import NeedsHumanEscalation      # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def load_email(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    email_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(__file__).parent / "sample_email.txt"
    )
    if not email_path.exists():
        print(f"[ERR] email file not found: {email_path}", file=sys.stderr)
        return 2

    email_body = load_email(email_path)
    sender_hint = os.getenv("SENDER_HINT") or None     # 邮箱被脱敏时从外部注入
    print(f"[demo] reading email from {email_path}")
    print(f"[demo] email length: {len(email_body)} chars")
    print(f"[demo] sender_hint: {sender_hint or '(auto-extract from body)'}")
    print()

    # 凭证 / 部署预检
    try:
        client = AzureLLMClient()
    except KeyError as e:
        print(
            f"[ERR] missing env var {e}. Copy .env.example → .env and fill "
            f"in AZURE_OPENAI_API_KEY / DEPLOYMENT / ENDPOINT.",
            file=sys.stderr,
        )
        return 3

    agent = EmailAgent(llm=client)

    try:
        result = agent.handle_email(email_body, sender_hint=sender_hint)
    except NeedsHumanEscalation as e:
        print("⚠️  EmailAgent could not auto-resolve; route to human:")
        print(f"   reason: {e.reason}")
        print(f"   partial state:\n{indent(pretty(e.partial), '   ')}")
        return 1

    print("=" * 70)
    print("TRIAGE")
    print("=" * 70)
    print(pretty(result.triage.model_dump()))
    print()

    print("=" * 70)
    print("RETRIEVAL")
    print("=" * 70)
    print(pretty(result.retrieval.model_dump()))
    print()

    print("=" * 70)
    print("DRAFT (for human review — not sent)")
    print("=" * 70)
    print(pretty(result.draft.model_dump()))
    print()

    print("=" * 70)
    print("COMPLIANCE")
    print("=" * 70)
    print(pretty(result.compliance.model_dump()))
    print()

    print("=" * 70)
    print("AUDIT (per-call token / cache stats)")
    print("=" * 70)
    print(pretty(result.audit))
    print()

    # 输出最终邮件草稿正文（给人看的）
    print("=" * 70)
    print(f"Subject: {result.draft.subject}")
    print("=" * 70)
    print(result.draft.body_md)
    print(f"\nNext step: {result.draft.next_step}")
    print()
    print(f"Human review required by compliance: {result.compliance.human_review_required}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
