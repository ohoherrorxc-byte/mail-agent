"""
build_v3_xlsx.py — 从 文件库/回复模版代码.docx 里的 Node.js rows 数组
抽出 iSMART 自动回复模板，生成 v3 xlsx 模板（与现有 v1/v2 同结构）。

输出：项目根 /iSMART售后服务邮箱回复方法_v3_<date>.xlsx
      （template_reply.load_xlsx glob 自动加载）

字段映射：
  docx rows[*]              → xlsx 列
  ─────────────────────────────────────
  id                         → (audit only, 不入表)
  l1 / l2                    → Category (l1 + "/" + l2)
  scene                      → Description
  keywords                   → (audit only, 留给未来检索)
  auto                       → (audit only)
  required                   → (audit only)
  template                   → Template (邮件正文 + Dear Customer + 签名)
  next                       → (audit only)
  human                      → (audit only)
  ban                        → (audit only)
  source                     → (audit only)

xlsx 三列（与 v1/v2 对齐，template_reply.load_xlsx 期望）：
  - Category
  - Template
  - Description

docx → xlsx 的语义差距：
  - v1/v2 是英文邮件草稿（"Dear customer,..."），无 Dear/sign wrapper
  - docx 是 Node mail() 函数包裹的完整邮件（"Dear Customer,\n\n<正文>\n\nBest regards,\niSMART Service Team"）
  - 我们 **保留 docx 的完整格式**，让 v3 模板直接产出可发邮件；与 v1/v2 不混用

用法：
  python3 scripts/build_v3_xlsx.py
  python3 scripts/build_v3_xlsx.py --docx 文件库/回复模版代码.docx --out iSMART售后服务邮箱回复方法_v3_20260724.xlsx
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from html import unescape
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCX = ROOT / "文件库" / "回复模版代码.docx"


# ── docx 文本提取 ───────────────────────────────────────────────

def extract_docx_text(docx_path: Path) -> str:
    """从 .docx (zip + word/document.xml) 提取纯文本。"""
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
    return unescape(" ".join(texts))


# ── rows 数组解析 ──────────────────────────────────────────────

def find_rows_array(script: str) -> str:
    """找 `const rows = [` 之后的数组文本（不含收尾 ']'，docx 漏写了）。"""
    m = re.search(r"const\s+rows\s*=\s*\[", script)
    if not m:
        raise ValueError("docx 脚本里找不到 `const rows = [`")
    return script[m.end():]


def split_row_objects(text: str) -> list[str]:
    """栈式切分顶层 {...}，跳过字符串字面量。"""
    rows: list[str] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] != "{":
            i += 1
        if i >= n:
            break
        depth, j = 1, i + 1
        in_str, esc = False, False
        while j < n and depth > 0:
            c = text[j]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c in ('"', "'", "`"):
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
            j += 1
        rows.append(text[i:j])
        i = j
    return rows


def _decode_js_string(raw: str) -> str:
    """把 JS 字符串字面量 (\\n \\t \\\" ...) 还原成 Python str。

    关键事实：
      - JS 字符串字面量里的非 ASCII 字符（中文、emoji 等）按 UTF-8 编码的原始
        字面字符出现，不是 \\uXXXX 转义。
      - 只有显式的 \\n \\t \\r \\" \\' \\\\ 等是 escape。

    因此正确做法：手解 escape，不调 unicode_escape（它会把所有非 ASCII 当
    转义处理，破坏中文）。
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        # escape 序列
        if i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "r":
            out.append("\r")
        elif nxt in ('"', "'"):
            out.append(nxt)
        elif nxt == "\\":
            out.append("\\")
        elif nxt == "0":
            out.append("\0")
        elif nxt == "b":
            out.append("\b")
        elif nxt == "f":
            out.append("\f")
        elif nxt == "v":
            out.append("\v")
        else:
            # 未知 escape：原样保留 \\X
            out.append(c)
            out.append(nxt)
        i += 2
    return "".join(out)


def _split_value_after_key(seg: str) -> str:
    """剥掉 `key :` 前缀，但保留 mail( "..." ) / "..." / `...` 完整。"""
    if ":" in seg:
        return seg.split(":", 1)[1].strip()
    return seg.strip()


def parse_row(row_text: str) -> dict[str, str]:
    """从 `{ key : value, key : value, ... }` 文本提字段。

    用 regex 一次性 match 所有 `key : value`，value 支持三种形态：
      - string:        "..."  / '...'  / `...`
      - func call:     name ( string )
    """
    out: dict[str, str] = {}

    # 一个 value 模式："string" / 'string' / `string` / name ( string )
    STRING = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
    VALUE = rf'(?:{STRING}|\w+\s*\(\s*{STRING}\s*\))'

    pattern = re.compile(
        rf'\s*(\w+)\s*:\s*({VALUE})',
        re.UNICODE,
    )

    for m in pattern.finditer(row_text):
        key = m.group(1)
        raw_val = m.group(2)
        out[key] = _normalize_value(raw_val)

    return out


def _normalize_value(raw: str) -> str:
    """把 match 出来的 value 标准化：去掉包裹（"..." 或 mail("...")）、解 escape。

    原始 docx 脚本里 value 形态：
      - "..."                → 简单字符串
      - mail ( "..." )       → Node 函数调用，正常有右括号；函数体是邮件正文
      - mail ( "..."         → docx 漏写右括号（已知 12 行里至少出现一次），
                                容忍：找到字符串字面量就提出来，不强制右括号

    docx 顶层定义了 mail(body) = `Dear Customer,\n\n${body}\n\nBest regards,\niSMART Service Team`，
    本函数对 func call 形态，模拟 mail() 重新包裹，让 xlsx 拿到的是完整邮件。
    """
    s = raw.strip()
    is_func_call = False

    # 1) 完整 func call：name ( "..." )
    m_func = re.match(
        r'(\w+)\s*\(\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)\s*\)\s*$',
        s, re.S,
    )
    if m_func:
        is_func_call = True
        s = m_func.group(2)
    # 2) 残缺 func call：name ( "..."  无右括号
    elif re.match(r'\w+\s*\(\s*"', s):
        m_partial = re.match(
            r'(\w+)\s*\(\s*("(?:\\.|[^"\\])*")',
            s, re.S,
        )
        if m_partial:
            is_func_call = True
            s = m_partial.group(2)

    # 3) 去首尾引号
    if len(s) >= 2 and s[0] in ('"', "'", "`") and s[-1] == s[0]:
        s = s[1:-1]

    body = _decode_js_string(s)

    if is_func_call:
        # 模拟 docx 顶层的 mail(body)：Dear Customer + body + 签名
        # 注：docx 是英文邮件草稿；与 v1/v2 中文模板并存不冲突。
        return f"Dear Customer,\n\n{body}\n\nBest regards,\niSMART Service Team"
    return body


def _coerce_value(s: str) -> str:
    """字符串字面量 → 反转义；其它原样。"""
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")) or \
       (s.startswith("`") and s.endswith("`")):
        inner = s[1:-1]
        return _decode_js_string(inner)
    return s


def parse_rows(script: str) -> list[dict[str, str]]:
    """主入口：脚本文本 → rows 列表。"""
    arr_text = find_rows_array(script)
    raw_rows = split_row_objects(arr_text)
    rows = [parse_row(r) for r in raw_rows]
    return [r for r in rows if r]


# 保留旧入口供调试用
def _split_value_after_key(seg: str) -> str:
    if ":" in seg:
        return seg.split(":", 1)[1].strip()
    return seg.strip()


def _coerce_value(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")) or \
       (s.startswith("`") and s.endswith("`")):
        inner = s[1:-1]
        return _decode_js_string(inner)
    return s


# ── xlsx 生成 ──────────────────────────────────────────────────

XLSX_COLUMNS = ["Category", "Template", "Description"]


def write_xlsx(rows: list[dict[str, str]], out_path: Path) -> int:
    """写入 xlsx；返回写入行数。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "templates"
    ws.append(XLSX_COLUMNS)

    written = 0
    for r in rows:
        l1 = (r.get("l1") or "").strip()
        l2 = (r.get("l2") or "").strip()
        scene = (r.get("scene") or "").strip()
        tmpl = (r.get("template") or "").strip()
        if not tmpl:
            continue  # 没有 template 文本的 row 跳过
        category = f"{l1}/{l2}" if l2 else l1
        ws.append([category, tmpl, scene])
        written += 1

    # 列宽
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 80
    ws.column_dimensions["C"].width = 60
    # 简单冻结首行
    ws.freeze_panes = "A2"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return written


# ── 入口 ───────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--docx", type=Path, default=DEFAULT_DOCX,
        help="docx 路径（默认 文件库/回复模版代码.docx）",
    )
    ap.add_argument(
        "--out", type=Path, default=None,
        help="输出 xlsx 路径（默认 iSMART售后服务邮箱回复方法_v3_<今天>.xlsx 到项目根）",
    )
    args = ap.parse_args()

    if not args.docx.exists():
        raise SystemExit(f"docx 不存在: {args.docx}")

    if args.out is None:
        from datetime import date
        out = ROOT / f"iSMART售后服务邮箱回复方法_v3_{date.today().isoformat()}.xlsx"
    else:
        out = args.out

    text = extract_docx_text(args.docx)
    rows = parse_rows(text)
    n = write_xlsx(rows, out)

    # 摘要
    by_l1: dict[str, int] = {}
    for r in rows:
        l1 = (r.get("l1") or "(empty)").strip() or "(empty)"
        by_l1[l1] = by_l1.get(l1, 0) + 1

    print(f"docx: {args.docx}")
    print(f"rows parsed: {len(rows)}")
    print(f"xlsx written: {n} ({out})")
    print(f"by l1:")
    for k, v in sorted(by_l1.items()):
        print(f"  {k:30s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())