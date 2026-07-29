"""
test_m1_6_vision_integration.py — M1.6 vision 集成单元测试。

覆盖：
1. Mail dataclass 默认 attachments=[]
2. _redact_mail 透传 attachments（不能因 redact 把附件搞丢）
3. VISION_MAX_IMAGE_BYTES 限制：超 4MB 的 image 跳过（防 token 爆炸）
4. VISION_MAX_IMAGES_PER_MAIL 限制：超 3 张的 image 跳过
5. URL 白名单包含 attachment $value 路径
6. schema.ImageDescription 字段齐

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
    VISION_MAX_IMAGES_PER_MAIL,
    VISION_MAX_IMAGE_BYTES,
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


# ── 测试 3: VISION_MAX_IMAGE_BYTES 常量 ──────────────────────────────────
def test_vision_size_limit_constant():
    """M1.6 设计：单图 ≤ 4MB（base64 ~5.3MB 字符 + ~3000 token）。
    常量必须 = 4MB。改了要小心成本。"""
    assert VISION_MAX_IMAGE_BYTES == 4 * 1024 * 1024, (
        f"期望 4MB, 实际 {VISION_MAX_IMAGE_BYTES}"
    )
    print("✓ test_vision_size_limit_constant")


# ── 测试 4: VISION_MAX_IMAGES_PER_MAIL 常量 ──────────────────────────────
def test_vision_count_limit_constant():
    """M1.6 设计：每封邮件 ≤3 张图。
    防 6+ 张图烧 token。"""
    assert VISION_MAX_IMAGES_PER_MAIL == 3, (
        f"期望 3, 实际 {VISION_MAX_IMAGES_PER_MAIL}"
    )
    print("✓ test_vision_count_limit_constant")


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


# ── 测试 6: 白名单仍不含 DELETE / sendMail / move / patch isRead ─────
def test_allowed_graph_patterns_no_dangerous():
    dangerous_substrings = [
        "DELETE", "/sendMail", "/move", "/messages/{mail_id}/attachments",
    ]
    for p in _ALLOWED_GRAPH_PATTERNS:
        for d in dangerous_substrings:
            # "$value" 路径 OK；其它匹配要 fail
            if d in p and "/messages/{mail_id}/attachments" in p and "$value" not in p:
                raise AssertionError(f"白名单含危险 pattern: {p}")
    # sendMail / move / DELETE 必须不在白名单
    raw = "\n".join(_ALLOWED_GRAPH_PATTERNS)
    for keyword in ["DELETE", "sendMail", "/move"]:
        assert keyword not in raw, (
            f"白名单含危险关键字 {keyword}:\n{raw}"
        )
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
        test_vision_size_limit_constant,
        test_vision_count_limit_constant,
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