"""
test_m1_6_vision_integration.py — M1.6 vision 集成单元测试。

覆盖：
1. Mail dataclass 默认 attachments=[]
2. _redact_mail 透传 attachments（不能因 redact 把附件搞丢）
3. 图片不按体积/张数预跳过（下不下来或 vision 失败才跳）
4. URL 白名单包含 attachment $value 路径
5. schema.ImageDescription 字段齐

不需要真连 Graph / Azure OpenAI —— 全本地 mock。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.outlook_drafts import (
    Mail,
    MailboxSafetyError,
    _ALLOWED_GRAPH_PATTERNS,
    _redact_mail,
)


# ── 测试 1: Mail dataclass 默认 attachments=[] ──────────────────────────
def test_mail_default_attachments_empty():
    m = Mail(
        id="x", subject="s", sender="a@b.com",
        received_at="2026-01-01", body_text="hi", is_read=False,
    )
    assert m.attachments == [], f"期望 [] 实际 {m.attachments}"
    print("✓ test_mail_default_attachments_empty")


# ── 测试 2: _redact_mail 透传 attachments ────────────────────────────────
def test_redact_mail_passes_attachments():
    """attachments 不能因 redact 步骤被丢。
    redact 改的是 subject/body/sender；attachments 是元数据，本身不含 PII，
    无须 redact，但必须透传（process_mail 后续要拿来做 vision）。"""
    atts = [{"id": "ATT1", "name": "foo.png", "contentType": "image/png",
             "size": 1024, "isInline": False}]
    # 用真实 17 字符 VIN（在 regex [A-HJ-NPR-Z0-9]{17} 范围内）
    real_vin = "LSVAB12345678901Z"   # 17 字符，无 I/O/Q
    m = Mail(
        id="x", subject=f"my car {real_vin}",
        sender="user_xxx@example.com", received_at="2026-01-01",
        body_text=f"my car {real_vin} broken", is_read=False,
        attachments=atts,
    )
    m2, counts = _redact_mail(m)
    assert m2.attachments == atts, (
        f"redact 后 attachments 应保持不变，"
        f"实际 {m2.attachments} vs {atts}"
    )
    # counts 应包含 VIN redact 命中（subject + body 各 1 次）
    assert counts["vin"] >= 2, f"期望 vin>=2 (subject+body) 实际 {counts}"
    assert real_vin not in m2.body_text, "VIN 应被 redact 掉"
    print("✓ test_redact_mail_passes_attachments")


# ── 测试 3: 图片不按体积/张数预跳过 ──────────────────────────────────
def test_vision_reads_all_images_no_prefilter():
    from unittest.mock import MagicMock
    from agent.outlook_drafts import GraphClient, Mail, _extract_image_descriptions
    from agent.schemas import ImageDescription

    g = MagicMock(spec=GraphClient)
    g.download_attachment = MagicMock(return_value=b"\xff\xd8fake")
    llm = MagicMock()
    llm.invoke_json_with_images = MagicMock(
        return_value=(
            ImageDescription(
                description="ok", app_name="none",
                error_text=None, error_code=None, is_ismart_app=False,
            ),
            None,
        )
    )
    mail = Mail(
        id="m", subject="s", sender="a@x.com",
        received_at="", body_text="", is_read=False,
        attachments=[
            {"id": "a1", "name": "big.jpg", "contentType": "image/jpeg",
             "size": 20 * 1024 * 1024, "isInline": False},
            {"id": "a2", "name": "two.png", "contentType": "image/png",
             "size": 100, "isInline": False},
            {"id": "a3", "name": "three.jpg", "contentType": "image/jpeg",
             "size": 100, "isInline": False},
            {"id": "a4", "name": "four.jpg", "contentType": "image/jpeg",
             "size": 100, "isInline": False},
        ],
    )
    out = _extract_image_descriptions(mail, g, llm)
    assert g.download_attachment.call_count == 4
    assert len(out) == 4
    print("✓ test_vision_reads_all_images_no_prefilter")


# ── 测试 5: URL 白名单包含 attachment $value 路径 ──────────────────────
def test_allowed_graph_patterns_has_attachment_value():
    """M1.6 加了 attachment $value 路径到白名单。
    没这条白名单，download_attachment 会被 _request() 拒。"""
    has_attachment = any(
        "attachments" in p and "$value" in p
        for p in _ALLOWED_GRAPH_PATTERNS
    )
    assert has_attachment, (
        f"_ALLOWED_GRAPH_PATTERNS 缺 attachment $value 路径：\n"
        f"{_ALLOWED_GRAPH_PATTERNS}"
    )
    print("✓ test_allowed_graph_patterns_has_attachment_value")


# ── 测试 6: 白名单仍不含 sendMail / move；DELETE 仅草稿附件 ─────
def test_allowed_graph_patterns_no_dangerous():
    raw = "\n".join(_ALLOWED_GRAPH_PATTERNS)
    assert "sendMail" not in raw
    assert "/move" not in raw
    for p in _ALLOWED_GRAPH_PATTERNS:
        if p.startswith("DELETE"):
            assert "/attachments/" in p and "{draft_id}" in p, p
    print("✓ test_allowed_graph_patterns_no_dangerous")


# ── 测试 7: ImageDescription schema 字段齐 ──────────────────────────────
def test_image_description_schema():
    from agent.schemas import ImageDescription
    expected = {"description", "app_name", "error_text", "error_code", "is_ismart_app"}
    actual = set(ImageDescription.model_fields.keys())
    assert expected == actual, (
        f"schema 字段不匹配\n  期望 {expected}\n  实际 {actual}"
    )
    print("✓ test_image_description_schema")


# ── main ─────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        test_mail_default_attachments_empty,
        test_redact_mail_passes_attachments,
        test_vision_reads_all_images_no_prefilter,
        test_allowed_graph_patterns_has_attachment_value,
        test_allowed_graph_patterns_no_dangerous,
        test_image_description_schema,
    ]
    for t in tests:
        t()
    print(f"\n✅ {len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())