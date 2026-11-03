"""10/24–今天：按现行规则重打「问题:」。只 PATCH categories。"""
import fcntl
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/Users/uxoxsn/Desktop/售后运维/mail-agent")
from agent import _env as env
from agent.foreign_draft_sweep import ensure_trust_store
from agent.mail_tags import (
    ISSUE_PREFIX,
    _SKIP_L2_SCORE,
    align_issue_tags_to_red,
    canonicalize_issue_l2,
    extract_known_issue_type,
    human_issue_display_names,
    issue_tag,
)
from agent.outlook_drafts import GraphClient, _html_to_text, get_token
import requests

env.load_env()
ensure_trust_store()
lock_path = os.path.expanduser("~/.cache/emailagent/outlook_drafts.lock")
if os.path.exists(lock_path):
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    except BlockingIOError:
        raise SystemExit("scan lock held")
    finally:
        os.close(fd)

token = get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"])
user = os.environ["MAILBOX_USER"]
g = GraphClient(token, user_id=user)
base = f"https://graph.microsoft.com/v1.0/users/{user}"
headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
session = requests.Session()


def get(url, params=None):
    last = 0
    for attempt in range(8):
        r = session.get(url, headers=headers, params=params, timeout=90)
        last = r.status_code
        if r.status_code in (429, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After") or min(30, 2 ** attempt)))
            continue
        r.raise_for_status()
        return r.json()
    raise SystemExit(f"graph {last}")


def desired(existing: list[str], body_text: str) -> list[str]:
    if human_issue_display_names(existing):
        return align_issue_tags_to_red(existing)
    issues = [c for c in existing if c.startswith(ISSUE_PREFIX)]
    if not issues:
        return existing
    extracted = canonicalize_issue_l2(extract_known_issue_type(body_text))
    bare = []
    for tag in issues:
        name = tag[len(ISSUE_PREFIX):]
        if name not in _SKIP_L2_SCORE:
            bare.append(name)
    facts = None
    if len(bare) == 1:
        facts = canonicalize_issue_l2(bare[0])
        if facts == "App-车控车态相关" and extracted == "App-绑车解绑相关":
            facts = extracted
        if extracted == "iSMART-支持区域":
            facts = extracted
        if facts == "FICM-CPAA相关" and extracted == "App-车控车态相关":
            facts = extracted
    elif extracted:
        facts = extracted
    kept = [c for c in existing if not c.startswith(ISSUE_PREFIX)]
    if facts:
        tag = issue_tag(facts)
        if tag and tag not in kept:
            kept.append(tag)
    return kept


since = "2026-10-23T16:00:00Z"  # 10-24 00:00 CST
until = "2026-10-28T16:00:00Z"  # 10-29 00:00 CST
url = f"{base}/messages"
params = {
    "$select": "id,categories,body",
    "$filter": f"receivedDateTime ge {since} and receivedDateTime lt {until}",
    "$orderby": "receivedDateTime desc",
    "$top": "50",
}
seen = changed = same = errors = 0
reasons = Counter()
pages = 0
while url:
    data = get(url, params)
    params = None
    pages += 1
    for m in data.get("value") or []:
        seen += 1
        cats = [str(c).strip() for c in (m.get("categories") or []) if str(c).strip()]
        body = (m.get("body") or {})
        content = body.get("content") or ""
        if (body.get("contentType") or "").lower() == "html":
            content = _html_to_text(content)
        new = desired(cats, content)
        old_issues = [c for c in cats if c.startswith(ISSUE_PREFIX)]
        new_issues = [c for c in new if c.startswith(ISSUE_PREFIX)]
        if set(new_issues) == set(old_issues) and set(new) == set(cats):
            same += 1
            continue
        if set(c for c in new if not c.startswith(ISSUE_PREFIX)) != set(
            c for c in cats if not c.startswith(ISSUE_PREFIX)
        ):
            errors += 1
            continue
        try:
            for tag in new_issues:
                g.ensure_issue_master_category(tag)
            g.set_categories(m["id"], new)
            changed += 1
            reasons[f"{' | '.join(old_issues) or '(无)'} -> {' | '.join(new_issues) or '(去掉)'}"] += 1
        except Exception as e:
            errors += 1
            print("ERR", type(e).__name__, getattr(getattr(e, "response", None), "status_code", ""), flush=True)
    url = data.get("@odata.nextLink")
    if pages % 10 == 0:
        print(f"pages={pages} seen={seen} changed={changed} same={same} err={errors}", flush=True)

print(f"done pages={pages} seen={seen} changed={changed} same={same} err={errors}")
print("TOP")
for k, n in reasons.most_common(20):
    print(f"{n:4} {k}")
