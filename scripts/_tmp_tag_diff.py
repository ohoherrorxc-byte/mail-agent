"""One-off comparison of red tags vs 问题: tags. Delete after use."""
import sys
from collections import Counter

sys.path.insert(0, "/Users/uxoxsn/Desktop/售后运维/mail-agent")
from openpyxl import load_workbook
from agent.mail_tags import (
    _HUMAN_ISSUE_L2,
    _KNOWN_L2,
    canonicalize_issue_l2,
)

SRC = "/Users/uxoxsn/Desktop/红标加紫标或问题标_20260801-20261027.xlsx"
OUT = "/Users/uxoxsn/Desktop/售后运维/mail-agent/scripts/_tmp_tag_diff.txt"


def parts(s):
    return [p.strip() for p in str(s or "").split("；") if p.strip()]


def canon(name):
    return canonicalize_issue_l2(name) or name


def main() -> None:
    wb = load_workbook(SRC, read_only=True)
    ws = wb.active
    exact = alias_only = conflict = no_issue = multi = 0
    pairs = Counter()
    alias_pairs = Counter()
    reds_seen = Counter()
    unknown_red = Counter()
    known = _HUMAN_ISSUE_L2 | _KNOWN_L2
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        reds = parts(row[5])
        issues = parts(row[7])
        if len(reds) > 1:
            multi += 1
        for r in reds:
            reds_seen[r] += 1
            if r not in known and canon(r) not in known:
                unknown_red[r] += 1
        bare = [i[3:] if i.startswith("问题:") else i for i in issues]
        if not bare:
            no_issue += 1
            continue
        red_c = {canon(r) for r in reds}
        if set(bare) <= set(reds) and set(reds) <= set(bare):
            exact += 1
            continue
        if set(bare) <= set(reds):
            # every 问题 name is some red, but a red has no 问题 twin
            alias_only += 1
            alias_pairs[("missing_twin", tuple(sorted(set(reds) - set(bare))))] += 1
            continue
        iss_c = {canon(b) for b in bare}
        if iss_c & red_c or set(bare) & set(reds):
            alias_only += 1
            for b in bare:
                if b not in reds and canon(b) in red_c:
                    alias_pairs[(b, canon(b))] += 1
            continue
        conflict += 1
        for b in bare:
            pairs[(reds[0], canon(b))] += 1
    lines = [
        f"exact_same_text {exact}",
        f"alias_or_partial {alias_only}",
        f"conflict {conflict}",
        f"no_issue {no_issue}",
        f"multi_red {multi}",
        "UNKNOWN_RED",
    ]
    for name, n in unknown_red.most_common():
        lines.append(f"{n:4} {name}")
    lines.append("ALIAS")
    for k, n in alias_pairs.most_common(30):
        lines.append(f"{n:4} {k}")
    lines.append("CONFLICT")
    for (red, ic), n in pairs.most_common(40):
        lines.append(f"{n:4} {red} || {ic}")
    lines.append("RED_FREQ")
    for name, n in reds_seen.most_common():
        lines.append(f"{n:4} {name}")
    open(OUT, "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
