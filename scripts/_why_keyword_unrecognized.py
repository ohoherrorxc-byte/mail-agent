"""只统计「未识别（已有关键词）」的表单类型和描述形态。不写客户原文。"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import _env as _env_loader
from agent.foreign_draft_sweep import ensure_trust_store
from agent.outlook_drafts import GraphClient, _html_to_text, get_token

CAT = "未识别（已有关键词）"
_TYPE_RE = re.compile(
    r"(?im)^[•\-\*]?\s*(?:Type of problem|Problem Type|Issue Type|"
    r"Tipo de problema|Art des Problems|Tipo di problema|"
    r"Type de problème|Type de probleme|"
    r"Problemtyp|Probleemtype|Type probleem)[:\s]+(.+)$"
)
_DESC_RE = re.compile(
    r"(?is)(?:Description of (?:the )?(?:issues?|problems?)|"
    r"Problem description|Problembeschreibung|Beschreibung der Probleme|"
    r"Descrizione dei problemi|Descripción de los problemas|"
    r"Description des problèmes|Beschrijving van (?:de |het )?probleem|"
    r"Beskrivelse av problemet|Beskrivning av problemen|"
    r"Opis problemów|Popis problému|A probléma leírása|"
    r"Kuvaus ongelmista)[:\s]*(.+)$"
)


def _get_json(g, path, params=None):
    last = 0
    for attempt in range(6):
        resp = g._request("GET", path, params=params)
        last = resp.status_code
        if resp.status_code in (429, 502, 503, 504):
            time.sleep(min(20, 2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"status={last}")


def _next(link: str) -> str:
    parsed = urlparse(link)
    path = parsed.path
    if "/v1.0" in path:
        path = path.split("/v1.0", 1)[1]
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _clean(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text or "")
    text = re.sub(r"\b[A-HJ-NPR-Z0-9]{17}\b", " ", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", " ", text)
    text = re.sub(r"\+?\d[\d\s\-()]{7,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _desc(body: str) -> str:
    m = _DESC_RE.search(body or "")
    chunk = m.group(1) if m else (body or "")
    chunk = re.split(
        r"(?i)i can read the latest privacy|by sending this email|"
        r"ich kann die neueste|en envoyant cet e-mail|"
        r"privacy policy|datenschutz",
        chunk,
        maxsplit=1,
    )[0]
    return _clean(chunk)


def _bucket(desc: str, issue: str) -> str:
    d = (desc or "").lower()
    if re.search(r"error\s*59", d) or "error 59" in issue.lower():
        return "Error 59"
    if re.search(r"\berror\s*\d+", d):
        return "其它报错编号"
    if re.search(r"notification bubble|don't get any messages|do not get any messages|no messages in the app|keine nachrichten|aucun message", d):
        return "收不到消息但有角标"
    if re.search(r"previous owner|ancien propriétaire|vorbesitzer|propietario anterior|ex owner|former owner", d):
        return "前车主"
    if re.search(r"cannot bind|can't bind|unable to bind|unbind|desvinc|entkopp|lier le véhicule|bind the", d):
        return "绑车或解绑"
    if re.search(r"password|login|log in|sign in|account|mot de passe|passwort|contraseña", d):
        return "账号或登录"
    if re.search(r"digital key|bluetooth key|clé numérique|digitaler schlüssel", d):
        return "数字钥匙"
    if re.search(r"charg|laden|recharg", d):
        return "充电"
    if re.search(r"carplay|android auto", d):
        return "CarPlay或Android Auto"
    if re.search(r"map|navigation|navig", d):
        return "导航或地图"
    if re.search(r"radio", d):
        return "收音机"
    if re.search(r"update|reinstall|aggiorn", d):
        return "更新或重装"
    if re.search(r"privacy|datenschutz|confidential", d):
        return "隐私"
    if not d or len(d) < 25:
        return "描述很短"
    return "有描述但没对上上面这些类"


def main() -> int:
    _env_loader.load_env()
    ensure_trust_store()
    g = GraphClient(
        get_token(os.environ["AZURE_AD_TENANT_ID"], os.environ["AZURE_AD_CLIENT_ID"]),
        os.environ.get("MAILBOX_USER", "me"),
    )
    escaped = CAT.replace("'", "''")
    path = f"/users/{g.user_id}/messages"
    params = {
        "$filter": f"categories/any(c:c eq '{escaped}')",
        "$select": "id,subject,parentFolderId,categories",
        "$top": "50",
    }
    g.session.headers["ConsistencyLevel"] = "eventual"
    ids = []
    while path:
        data = _get_json(g, path, params)
        params = None
        for item in data.get("value") or []:
            if item.get("id"):
                ids.append(item["id"])
        link = data.get("@odata.nextLink")
        path = _next(link) if link else ""
    types = Counter()
    buckets = Counter()
    picks = Counter()
    pair = Counter()
    from agent.mail_tags import extract_known_issue_type

    inbox_id = _get_json(g, f"/users/{g.user_id}/mailFolders/Inbox", {"$select": "id"}).get("id")
    sent_id = _get_json(g, f"/users/{g.user_id}/mailFolders/sentitems", {"$select": "id"}).get("id")
    where = Counter()
    for i, mid in enumerate(ids, 1):
        msg = _get_json(
            g,
            f"/users/{g.user_id}/messages/{mid}",
            {"$select": "subject,body,parentFolderId"},
        )
        folder = str(msg.get("parentFolderId") or "")
        place = "收件箱" if folder == inbox_id else "已发送" if folder == sent_id else "其它"
        where[place] += 1
        body = (msg.get("body") or {}).get("content") or ""
        if "<" in body:
            body = _html_to_text(body)
        tm = _TYPE_RE.search(body)
        issue = _clean(tm.group(1) if tm else "") or "(没有读到问题类型)"
        if re.search(r"error\s*59", (msg.get("subject") or ""), re.I):
            issue = "Error 59（主题）"
        desc = _desc(body)
        b = _bucket(desc, issue)
        types[issue[:80]] += 1
        buckets[b] += 1
        l2 = extract_known_issue_type(body) or "(关键词也没定类)"
        picks[(place, l2)] += 1
        if i % 25 == 0:
            print(f"READ {i}", flush=True)
    print(f"N {len(ids)}", flush=True)
    print("WHERE", flush=True)
    for k, v in where.most_common():
        print(f"{v}\t{k}", flush=True)
    print("BUCKETS", flush=True)
    for k, v in buckets.most_common():
        print(f"{v}\t{k}", flush=True)
    print("L2", flush=True)
    for (place, l2), v in picks.most_common(30):
        print(f"{v}\t{place}\t{l2}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
