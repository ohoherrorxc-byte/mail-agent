#!/usr/bin/env python3
"""
attachment_vision_summary.py — 对 candidates.jsonl 里有图片附件的邮件，
下载附件 → 调用 GPT-5.4 vision 生成 1-2 句中文摘要 → 写回 candidates.jsonl。

Why needed:
  harvest 时只下 metadata (name/contentType/size)，不下 binary。
  但客户常在邮件里附报错截图，纯文字 hint 看不到错误码 / UI 状态。
  用 vision LLM 把截图转成文本片段，标的人就能在 chat_label.py 里看到
  "📎 screenshot 1: 错误码 59 + 中文错误页 ... "。

PII:
  - 附件二进制不落 LLM 之外的任何路径
  - LLM 输入仍是 raw image (Azure OpenAI 不存 data)
  - 输出存到 attachment_summaries[]，原 attachment 元数据保留
  - 二进制存到 eval/attachments/{message_id}/ (gitignored)，人可手动删

Usage:
  # 评所有 candidates（默认 candidates_merged.jsonl）
  python3 scripts/attachment_vision_summary.py

  # 限制条数（防刷）
  python3 scripts/attachment_vision_summary.py --limit 20

  # 强制重评（覆盖已有摘要）
  python3 scripts/attachment_vision_summary.py --force
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent

# ── env ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(ROOT))
from agent import _env as _env_loader  # noqa: E402
_env_loader.load_env()

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]
API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

ATTACH_DIR = ROOT / "eval" / "attachments"
ATTACH_DIR.mkdir(parents=True, exist_ok=True)

LOG = logging.getLogger("vision_summary")

# 支持的图片 contentType；其它（pdf/zip/docx）跳过
IMAGE_CT = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp", "image/bmp"}

# ── vision 调用 ─────────────────────────────────────────────────────

_VISION_PROMPT = (
    "你是 iSmart 海外售后邮件自动 triage 助手。这是客户邮件里附的一张截图。\n"
    "请用 1-2 句中文描述你看到的内容，重点：\n"
    "  - 错误码 / 错误文案（如有）\n"
    "  - UI 状态（按钮 / 弹窗 / loading / blank screen 等）\n"
    "  - 文字语言（如 'Chinese UI' / 'English error'）\n"
    "不要描述客户姓名 / 邮箱 / VIN / 车牌 / 地理位置等 PII（即使看见也用 <PII> 占位）。\n"
    "如果图里只有风景照或无关内容，回 'no relevant content'。"
)


def _call_vision(image_bytes: bytes, content_type: str) -> str:
    """调一次 vision API，返回 1-2 句中文摘要。失败返回 ''。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    url = (
        f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}"
        f"/chat/completions?api-version={API_VERSION}"
    )
    headers = {"Content-Type": "application/json", "api-key": API_KEY}
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            ],
        }],
        "max_completion_tokens": 200,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=60)
    if r.status_code != 200:
        LOG.warning("vision HTTP %s: %s", r.status_code, r.text[:200])
        return ""
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        return ""


# ── attachment 下载（GET /users/{u}/messages/{m}/attachments/{a}）───


class _GraphClient:
    """最小 Graph GET 客户端：client_credentials + download attachment binary。"""
    def __init__(self):
        self.token = self._get_token()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _get_token(self) -> str:
        tenant = os.environ["AZURE_AD_TENANT_ID"]
        client_id = os.environ["AZURE_AD_CLIENT_ID"]
        secret = os.environ["AZURE_AD_CLIENT_SECRET"]
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        r = requests.post(url, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": "https://graph.microsoft.com/.default",
        }, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def download(self, message_id: str, attachment_id: str) -> bytes:
        # URL-encode message_id and attachment_id (Graph 返回的 id 带 /)
        from urllib.parse import quote
        url = (
            f"{GRAPH_BASE}/users/{os.environ.get('MAILBOX_USER', 'me')}"
            f"/messages/{quote(message_id, safe='')}"
            f"/attachments/{quote(attachment_id, safe='')}"
        )
        # 注：contentBytes 不能 $select（Graph 会报 400 "Could not find
        # a property named 'contentBytes'"），直接 GET 即可，返回字段
        # 内含 contentBytes (base64)
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        return base64.b64decode(data["contentBytes"])


# ── 主流程 ──────────────────────────────────────────────────────────


def _process_record(rec: dict, client: _GraphClient, force: bool) -> bool:
    """处理单条 record；返回是否更新。"""
    if (rec.get("attachment_count") or 0) == 0:
        return False
    if not force and rec.get("attachment_summaries"):
        return False

    msg_id = rec["graph_message_id"]
    attachments = rec.get("attachments") or []
    if not attachments:
        # metadata-only harvest 没拿到 attachments 详情；只能按 attachment_count 跳过
        LOG.warning("[skip] %s: 无 attachment 元数据（需用 --attachments-meta 重 harvest）", msg_id)
        return False

    out_dir = ATTACH_DIR / msg_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict] = []
    for att in attachments:
        ct = (att.get("contentType") or "").lower()
        name = att.get("name") or "unknown"
        att_id = att.get("id") or ""
        if not ct.startswith("image/") and ct not in IMAGE_CT:
            summaries.append({"name": name, "contentType": ct, "summary": "(non-image, skipped)"})
            continue
        if not att_id:
            summaries.append({"name": name, "contentType": ct, "summary": "(no attachment id)"})
            continue
        # 下载
        try:
            img_bytes = client.download(msg_id, att_id)
            (out_dir / name).write_bytes(img_bytes)
        except Exception as e:
            LOG.warning("[dl-fail] %s/%s: %s", msg_id, name, e)
            summaries.append({"name": name, "contentType": ct, "summary": f"(download fail: {e})"})
            continue
        # vision
        try:
            s = _call_vision(img_bytes, ct)
        except Exception as e:
            LOG.warning("[vision-fail] %s/%s: %s", msg_id, name, e)
            s = f"(vision fail: {e})"
        summaries.append({"name": name, "contentType": ct, "size": len(img_bytes), "summary": s})
        time.sleep(0.2)  # rate-limit（4 parallel × 0.2s = 50 req/min，安全）

    rec["attachment_summaries"] = summaries
    LOG.info("[ok] %s  ← %d attachments", msg_id, len(summaries))
    return True


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default="eval/candidates_merged.jsonl",
                    help="输入 jsonl；写回同一文件。")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    ap.add_argument("--force", action="store_true", help="覆盖已有 attachment_summaries")
    ap.add_argument("--workers", type=int, default=4, help="并发")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    p = Path(args.candidates)
    recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    LOG.info("[vision] 加载 %d 条 from %s", len(recs), p)

    todo = [r for r in recs if (r.get("attachment_count") or 0) > 0]
    if not args.force:
        todo = [r for r in todo if not r.get("attachment_summaries")]
    if args.limit:
        todo = todo[:args.limit]
    LOG.info("[vision] 待处理 %d 条（%d 并发）", len(todo), args.workers)

    if not todo:
        LOG.info("[vision] 无待处理，结束")
        return 0

    client = _GraphClient()
    n_done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_process_record, r, client, args.force): r
                   for r in todo}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                if fut.result():
                    n_done += 1
            except Exception as e:
                LOG.error("[fail] %s: %s", r.get("graph_message_id"), e)

    # 写回
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    LOG.info("[vision] 完成 %d/%d；attachment_summaries 写入 %s", n_done, len(todo), p)
    LOG.info("[vision] 二进制落 %s (gitignored)", ATTACH_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
