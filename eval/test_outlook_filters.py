"""
test_outlook_filters.py — 验证 --message-ids / --from-filter / --subject-contains 三个过滤标志。

不连真 Graph：用 --mock-mail 模式 + 自带 JSON 跑过滤逻辑。
注意：--from-filter / --subject-contains / --message-ids 是真连 Outlook 才生效，
但我们这里用 mock 测试的是"按 ID 选邮件"和"按 subject/from 子串过滤"的语义
（在 main() 里的过滤逻辑本身，不走 Graph）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _mock_with(ids: list[str], senders: list[str], subjects: list[str]) -> Path:
    """生成临时 mock JSON 文件"""
    items = [
        {
            "subject":     s,
            "sender":      snd,
            "received_at": "2026-07-22T10:00:00Z",
            "body_text":   f"Test body for {s}",
        }
        for s, snd in zip(subjects, senders)
    ]
    p = Path("/tmp/_filter_mock.json")
    p.write_text(json.dumps(items), encoding="utf-8")
    return p


def run(args: list[str]) -> dict:
    """跑 outlook_drafts 子命令，解析 --out 写的 JSON 文件。"""
    out_path = Path("/tmp/_filter_out.json")
    out_path.write_text("{}")  # 清空
    full = ["python3", "-m", "agent.outlook_drafts",
            "--mock-mail", str(SCRIPTS / "mock_mail_smoke.json"),
            "--no-llm"] + args + ["--out", str(out_path)]
    subprocess.check_output(full, cwd=str(ROOT), stderr=subprocess.STDOUT)
    return json.loads(out_path.read_text())


def main() -> int:
    # mock_mail_smoke.json: 4 封
    #  1: "iSMART app — cannot bind vehicle (QR code not showing)"  user_001@example.com  → app_binding
    #  2: "Data plan question"                                       user_002@example.com  → ismart_data_plan
    #  3: "URGENT — keys locked inside vehicle"                     user_003@example.com  → ismart_digital_key
    #  4: "Going to take this to media"                             angry_user@example.com → SKIP P0

    cases = [
        # (label, extra_args, expected_total_mails, expected_subjects_or_none)
        # 注意：mock_mail_smoke.json 共 4 封，其中 1 封 P0 被 SKIP，
        # baseline 看到的是 4 封（3 drafted + 1 skipped）。
        ("baseline (no filter)",         [], 4, None),
        ("--from-filter user_001",       ["--from-filter", "user_001@example.com"], 1, ["iSMART app"]),
        ("--from-filter USER_002 (case-insensitive)",
                                        ["--from-filter", "USER_002@example.com"], 1, ["Data plan"]),
        ("--subject-contains urgent",    ["--subject-contains", "urgent"], 1, ["URGENT"]),
        ("--subject-contains nonexistent", ["--subject-contains", "xxx_no_match"], 0, []),
        ("--from + --subject (AND)",     ["--from-filter", "user_003@example.com",
                                          "--subject-contains", "URGENT"], 1, ["URGENT"]),
        ("--from mismatch + --subject match", ["--from-filter", "nobody@example.com",
                                               "--subject-contains", "URGENT"], 0, []),
        # --intent-allowlist 是 triage 后过滤，不减少邮件数
        ("--intent-allowlist app_binding",
                                        ["--intent-allowlist", "app_binding"], 4, ["iSMART app"]),
    ]

    print("=" * 70)
    print("Outlook Drafts Filter Feature Tests (mock mode)")
    print("=" * 70)
    passed = 0
    total = 0
    for label, args, expected_count, expected_subjects in cases:
        total += 1
        result = run(args)
        records = [r for r in result.get("records", [])
                   if not r.get("error") and not r.get("skipped")]
        skipped  = [r for r in result.get("records", []) if r.get("skipped")]
        n_after_filter = len(records) + len(skipped)
        ok = (n_after_filter == expected_count)
        if ok:
            passed += 1
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label:50s} → {n_after_filter} mails (expected {expected_count})")
        if expected_subjects and records + skipped:
            for r in records + skipped:
                subj = r.get("subject", "")[:40]
                if not any(s.lower() in subj.lower() for s in expected_subjects):
                    print(f"      ⚠ subject '{subj}' doesn't match expected {expected_subjects}")

    print("=" * 70)
    print(f"{passed}/{total} cases passed")

    extra = [
        ("--stop 无进程", test_stop_when_idle),
        ("默认写入台账路径", test_default_ledger_out_path),
        ("--every 不能配 mock", test_every_rejects_mock),
    ]
    for label, fn in extra:
        total += 1
        try:
            fn()
            passed += 1
            print(f"  ✓ {label}")
        except Exception as e:
            print(f"  ✗ {label}: {type(e).__name__}: {e}")
    print(f"{passed}/{total} cases passed (incl. scan cadence)")
    return 0 if passed == total else 1


def test_stop_when_idle():
    r = subprocess.run(
        ["python3", "-m", "agent.outlook_drafts", "--stop"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "没有在跑" in (r.stdout + r.stderr)


def test_default_ledger_out_path():
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from argparse import Namespace
    from agent.outlook_drafts import LEDGER_DIR, _resolve_scan_out_path

    args = Namespace(out=None, no_ledger=False, every=None)
    p = Path(_resolve_scan_out_path(args))
    assert p.parent == LEDGER_DIR
    assert p.name.startswith("outlook_scan_")
    assert p.suffix == ".json"
    args2 = Namespace(out=None, no_ledger=True, every=None)
    assert _resolve_scan_out_path(args2) is None


def test_every_rejects_mock():
    r = subprocess.run(
        ["python3", "-m", "agent.outlook_drafts",
         "--mock-mail", str(ROOT / "scripts" / "mock_mail_smoke.json"),
         "--every", "15", "--no-llm"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "--every" in (r.stderr + r.stdout)


if __name__ == "__main__":
    sys.exit(main())