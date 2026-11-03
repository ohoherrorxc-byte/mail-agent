"""把 8/1 至今已有「问题:」的信改成和人工红标同字。只 PATCH categories。"""
import sys
import time
from collections import Counter

sys.path.insert(0, "/Users/uxoxsn/Desktop/售后运维/mail-agent")
from agent import _env as env
from agent.foreign_draft_sweep import ensure_trust_store
from agent.mail_tags import ISSUE_PREFIX, align_issue_tags_to_red, human_issue_display_names
from agent.outlook_drafts import GraphClient, get_token
import requests

env.load_env()
ensure_trust_store()
import fcntl
import os
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
token = get_token(
    __import__("os").environ["AZURE_AD_TENANT_ID"],
    __import__("os").environ["AZURE_AD_CLIENT_ID"],
)
user = __import__("os").environ["MAILBOX_USER"]
g = GraphClient(token, user_id=user)
base = f"https://graph.microsoft.com/v1.0/users/{user}"
headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
session = requests.Session()


def get(url, params=None):
    for attempt in range(8):
        r = session.get(url, headers=headers, params=params, timeout=90)
        if r.status_code in (429, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After") or min(30, 2 ** attempt)))
            continue
        r.raise_for_status()
        return r.json()
    raise SystemExit(f"graph {r.status_code}")


since = "2026-07-31T16:00:00Z"
until = "2026-10-27T16:00:00Z"
url = f"{base}/messages"
params = {
    "$select": "id,categories",
    "$filter": f"receivedDateTime ge {since} and receivedDateTime lt {until}",
    "$orderby": "receivedDateTime desc",
    "$top": "100",
}
seen = changed = skipped = errors = 0
reasons = Counter()
pages = 0
while url:
    data = get(url, params)
    params = None
    pages += 1
    for m in data.get("value") or []:
        seen += 1
        cats = [str(c).strip() for c in (m.get("categories") or []) if str(c).strip()]
        issues = [c for c in cats if c.startswith(ISSUE_PREFIX)]
        reds = human_issue_display_names(cats)
        if not issues or not reds:
            continue
        aligned = align_issue_tags_to_red(cats)
        new_issues = [c for c in aligned if c.startswith(ISSUE_PREFIX)]
        if set(new_issues) == set(issues):
            skipped += 1
            continue
        # 只换问题: 集合。红标仍在。
        if set(c for c in aligned if not c.startswith(ISSUE_PREFIX)) != set(
            c for c in cats if not c.startswith(ISSUE_PREFIX)
        ):
            errors += 1
            continue
        mid = m.get("id")
        try:
            for tag in aligned:
                if tag.startswith(ISSUE_PREFIX):
                    g.ensure_issue_master_category(tag)
            g.set_categories(mid, aligned)
            changed += 1
            old = " | ".join(issues)
            new = " | ".join(c for c in aligned if c.startswith(ISSUE_PREFIX))
            reasons[f"{old} -> {new}"] += 1
        except Exception as e:
            errors += 1
            print("ERR", type(e).__name__, str(e)[:160], flush=True)
    url = data.get("@odata.nextLink")
    if pages % 20 == 0:
        print(f"pages={pages} seen={seen} changed={changed} same={skipped} err={errors}", flush=True)

print(f"done pages={pages} seen={seen} changed={changed} same={skipped} err={errors}")
print("TOP")
for k, n in reasons.most_common(15):
    print(f"{n:4} {k}")
