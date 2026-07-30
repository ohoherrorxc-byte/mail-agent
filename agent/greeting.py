"""
greeting.py — 草稿称谓个性化（"Dear customer," → "Dear Mr./Ms. {Name},"）。

设计目标：
  1. 不动 LLM prompt，不破坏 prompt cache 命中
  2. 模板路径与 LLM 路径共用同一函数
  3. 拿不到名字时降级为 "Dear customer,"，不抛错、不写空字符串
  4. 不引入时间承诺、不改其他内容
  5. PII 安全：只从 mail.sender 显示名提取，邮箱本身已 redact

策略（用户拍板 2026-07-24：统一用 Mr./Ms.，不区分性别）：
  - 有 first + last   → "Dear Mr./Ms. {Last},"
  - 只有 first name   → "Dear Mr./Ms. {First},"
  - 单 token 显示名   → "Dear Mr./Ms. {Token},"
  - 拿不到名字        → "Dear customer,"（保留默认）

不区分性别的好处：避免名字-性别映射错位（如外语名 / 跨文化名），也不维护
姓名-性别词表。
"""
from __future__ import annotations

import re
from typing import Optional


# 拿不到名字或名字可疑（占位词）→ 保留 "Dear customer,"
_PLACEHOLDER_FIRST_NAMES: frozenset[str] = frozenset({
    "customer", "user", "noreply", "no-reply", "admin", "support",
    "test", "unknown", "anonymous", "na", "n/a", "", "owner",
    "client", "subscriber", "member", "info", "sales", "no",
})


# ── 公开 API ───────────────────────────────────────────────────────

def extract_display_name(sender: str) -> Optional[str]:
    """从 mail.sender 提取显示名。

    支持：
      "John Smith <john@example.com>"  → "John Smith"
      "john@example.com"                → None（纯邮箱，缺显示名）
      "<john@example.com>"              → None
      ""                                → None
      "John Smith"                      → "John Smith"

    Returns: 显示名（如有），或 None。
    """
    if not sender:
        return None
    s = sender.strip()
    # "Name <email@...>" 形式
    m = re.match(r"^\s*[\"']?([^\"']+?)[\"']?\s*<[^>]+>\s*$", s)
    if m:
        return m.group(1).strip()
    # 纯邮箱 / 缺显示名
    if "@" in s and "<" not in s:
        return None
    if s.startswith("<") and s.endswith(">"):
        return None
    # 纯显示名（不含 @ / <）
    return s if s else None


def split_first_last(display_name: str) -> tuple[Optional[str], Optional[str]]:
    """显示名 → (first_name, last_name)。

    单 token（"Madonna"）→ ("Madonna", None)（只有 first）
    "John Smith"          → ("John", "Smith")
    "Mr. John Smith"      → ("John", "Smith")  —— 去称谓
    "Smith, John"         → ("John", "Smith")  —— 罕见格式
    """
    if not display_name:
        return None, None
    # 去常见英文称谓
    HONORIFICS = re.compile(
        r"^\s*(mr|mrs|ms|miss|mx|dr|prof|sir|madam|mme|mademoiselle)\.?\s+",
        re.IGNORECASE,
    )
    s = HONORIFICS.sub("", display_name).strip()
    if not s:
        return None, None
    # "Last, First" 罕见格式
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0], parts[0].split()[0]
    tokens = s.split()
    if len(tokens) == 1:
        return tokens[0], None   # 单 token 当 first name 用
    if len(tokens) >= 2:
        return tokens[0], tokens[-1]
    return None, None


def build_greeting(sender: str) -> str:
    """根据 mail.sender 生成完整称谓。

    Returns:
        "Dear Mr./Ms. {Last}," / "Dear Mr./Ms. {First} {Last}," /
        "Dear Mr./Ms. {First}," / "Dear Mr./Ms. {Single}," /
        "Dear customer,"

    调用方在草稿开头用一次 replace / 替换 "Dear customer," 即可。
    """
    display = extract_display_name(sender)
    if not display:
        return "Dear customer,"
    first, last = split_first_last(display)
    # 拿不到 first/last，或 first 是占位词 → 保留默认
    if not first or first.lower() in _PLACEHOLDER_FIRST_NAMES:
        return "Dear customer,"
    # 优先级：last > first + last > first > 单 token
    if last:
        return f"Dear Mr./Ms. {last},"
    return f"Dear Mr./Ms. {first},"


# ── 替换函数 ──────────────────────────────────────────────────────

# 兼容所有可能出现的 default greeting：xlsx 里清一色 "Dear customer,"，
# 但 LLM 起草可能写 "Dear Customer,"（大写 C）或 "Hi customer,"。
# 匹配不区分大小写；只替换"第一行的开头"，避免误伤正文里 "Dear customer
# service team" 之类的合法短语。
#
# 占位符 {{salutation}}（M2.5.4 引入）：模板正文写死"Dear Customer,"会被 LLM
# 起草时改写成 "Dear Valued Customer," / "Dear John," / "Hi there,"，原正则匹配
# 不到就漏网。改成 {{salutation}} 后由本函数确定性替换，LLM 起草也会被
# prompt 强制保留占位符。
_GREETING_PATTERNS: tuple[str, ...] = (
    "Dear Mr./Ms. ,",          # 已替换但 last 缺失
    "{{salutation}},",         # M2.5.4: 模板显式占位符（推荐写法）
    "Dear customer,",
    "Dear Customer,",
    "Hi customer,",
    "Hi Customer,",
    "Hello customer,",
    "Hello Customer,",
)


def personalize_greeting(draft_body: str, sender: str) -> str:
    """把草稿正文开头的默认称谓替换成发件人称谓。

    Args:
        draft_body: 草稿原文（来自模板或 LLM 起草）
        sender: mail.sender（已 redact 过邮箱）

    Returns:
        替换后的正文。拿不到名字时**原样返回**。
    """
    if not draft_body or not sender:
        return draft_body
    greeting = build_greeting(sender)
    if greeting == "Dear customer,":
        return draft_body   # 拿不到名字，不动草稿
    # 只替换第一次出现的默认称谓（开头）
    out = draft_body
    for pat in _GREETING_PATTERNS:
        # 大小写不敏感，且只命中一次
        idx = out.lower().find(pat.lower())
        if idx == 0:
            out = greeting + out[len(pat):]
            return out
        if idx > 0 and out[:idx].strip() == "":
            # 前面只允许空行
            out = greeting + out[idx + len(pat):]
            return out
    return out
