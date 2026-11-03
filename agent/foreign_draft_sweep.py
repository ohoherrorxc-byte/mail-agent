"""清掉草稿箱里不属于知识库的旧隐私套话。

只删未发送草稿。不删原信，不发送，不改知识库模板。
正文里还有手册步骤、编号清单等其它内容的草稿留着。

每两小时看一轮：
  python3 -m agent.foreign_draft_sweep --every 120
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import re
import ssl
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import _env as _env_loader
from .outlook_drafts import get_token

LOG = logging.getLogger("foreign_draft_sweep")
CACHE_DIR = Path.home() / ".cache" / "emailagent"
PID_FILE = CACHE_DIR / "foreign_draft_sweep.pid"
CA_BUNDLE = CACHE_DIR / "ca-bundle.pem"
_KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
)

_PRIVACY = "personal data will be used to monitor the status of your vehicle"
_ASK = "country/region, brand and vehicle identification number"

_BOILERPLATE = (
    "dear customer,",
    "dear mr./ms.",
    "thanks for your email.",
    "thank you for your email.",
    "thank you!",
    "best regards,",
    "kind regards,",
    "ismart-service team",
    "ismart service team",
    "since your feedback involves technical details, we need to collect your country/region, brand and vehicle identification number to better solve the issue.",
    "please provide your country/region, brand and vehicle identification number for further assistant.",
    "please provide your country/region, brand and vehicle identification number to better solve the issue.",
    "moreover, please provide your country/region, brand and vehicle identification number to query the vehicle's status in the database.",
    "your personal data will be used to monitor the status of your vehicle over the next several days and deleted right after our use, your personal data for after-sale troubleshooting will not be sold, neither retained.",
    "for more detail, you can find in our privacy notice as attached.",
    "and your consent to data processing for the purpose of the troubleshooting shall be regarded as granted, once your personal data for troubleshooting have been received,",
    "you can raise your complaint on personal data rights to the following mail,",
    "privacyoffice@ismart-service.com",
)


def ensure_trust_store(dest: Path | None = None) -> Path:
    """登录前装上 certifi 和 macOS 钥匙串里的根证书。

    定时器进程不带用户终端的证书路径。python.org 的 Python 默认信任库是空的，
    只认 certifi 时，钥匙串里已信任的颁发机构会校验失败。
    """
    dest = dest or CA_BUNDLE
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    try:
        import certifi
        text = Path(certifi.where()).read_text(errors="replace")
        if "BEGIN CERTIFICATE" in text:
            chunks.append(text)
    except Exception:
        LOG.warning("certifi 不可用，只使用钥匙串根证书")
    for keychain in _KEYCHAINS:
        if not Path(keychain).exists():
            continue
        try:
            got = subprocess.run(
                ["security", "find-certificate", "-a", "-p", keychain],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if got.returncode == 0 and "BEGIN CERTIFICATE" in got.stdout:
            chunks.append(got.stdout)
    if not chunks:
        raise RuntimeError("没有可用的 CA 证书，无法登录")
    dest.write_text("\n".join(chunks))
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    bundle = str(dest)
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ["REQUESTS_CA_BUNDLE"] = bundle
    os.environ["CURL_CA_BUNDLE"] = bundle

    def _context(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
        ctx = ssl.create_default_context(purpose, cafile=cafile, capath=capath, cadata=cadata)
        if cafile is None and capath is None and cadata is None:
            ctx.load_verify_locations(cafile=bundle)
        return ctx

    ssl._create_default_https_context = lambda: _context()
    return dest


def reply_plain(body: dict | None) -> str:
    """草稿回复区的纯文本。Outlook 引用块不算回复。"""
    content = ((body or {}).get("content")) or ""
    ctype = ((body or {}).get("contentType") or "").lower()
    if ctype == "html":
        cut = re.search(r'<div[^>]*\bid=["\']divRplyFwdMsg["\']', content, re.I)
        head = content[:cut.start()] if cut else content
        head = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", head)
        head = re.sub(r"(?i)<br\s*/?>", "\n", head)
        head = re.sub(r"(?i)</p>", "\n", head)
        head = re.sub(r"<[^>]+>", " ", head)
        text = html.unescape(head)
    else:
        text = content
    return re.sub(r"[ \t]+", " ", text).strip()


def is_foreign_privacy_reply(text: str) -> bool:
    """整段回复几乎只有「要国家/品牌/VIN + 旧隐私套话」。

    去掉套话后还剩一段别的说明（手册步骤、编号清单）则不是这一类。
    """
    low = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    if _PRIVACY not in low or _ASK not in low:
        return False
    leftover = low
    for bit in _BOILERPLATE:
        leftover = leftover.replace(bit, " ")
    leftover = re.sub(r"[^a-z0-9]+", " ", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip()
    return len(leftover) < 40


def _graph_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }


def sweep_once(*, token: str, user_id: str, dry_run: bool = False) -> dict[str, int]:
    """搜草稿箱，删掉确认是草稿且正文是旧套话的。返回计数。"""
    session = requests.Session()
    headers = _graph_headers(token)
    base = f"https://graph.microsoft.com/v1.0/users/{user_id}"
    folder = session.get(
        f"{base}/mailFolders/Drafts",
        headers=headers,
        params={"$select": "id"},
        timeout=60,
    )
    folder.raise_for_status()
    drafts_id = folder.json()["id"]

    url = f"{base}/mailFolders/Drafts/messages"
    params: dict[str, str] | None = {
        "$search": '"personal data will be used to monitor the status of your vehicle"',
        "$top": "50",
        "$select": "id,isDraft,parentFolderId,body",
    }
    seen = deleted = kept = skipped = 0
    while url:
        resp = session.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        params = None
        for item in data.get("value") or []:
            seen += 1
            text = reply_plain(item.get("body") or {})
            if not is_foreign_privacy_reply(text):
                kept += 1
                continue
            mid = item.get("id") or ""
            if not mid:
                skipped += 1
                continue
            check = session.get(
                f"{base}/messages/{mid}",
                headers=headers,
                params={"$select": "id,isDraft,parentFolderId"},
                timeout=60,
            )
            if check.status_code == 404:
                skipped += 1
                continue
            check.raise_for_status()
            got = check.json()
            if not got.get("isDraft") or got.get("parentFolderId") != drafts_id:
                skipped += 1
                LOG.warning("skip non-draft %s", mid[:16])
                continue
            if dry_run:
                deleted += 1
                continue
            gone = session.delete(f"{base}/messages/{mid}", headers=headers, timeout=60)
            if gone.status_code not in (204, 404):
                gone.raise_for_status()
            deleted += 1
        url = data.get("@odata.nextLink")
    LOG.info(
        "foreign privacy drafts: seen=%d deleted=%d kept=%d skipped=%d",
        seen, deleted, kept, skipped,
    )
    return {"seen": seen, "deleted": deleted, "kept": kept, "skipped": skipped}


def _already_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return pid != os.getpid()


def main(argv: list[str] | None = None) -> int:
    _env_loader.load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description="删除草稿箱里的旧隐私套话草稿")
    p.add_argument("--dry-run", action="store_true", help="只计数，不删除")
    p.add_argument(
        "--every",
        type=float,
        default=0,
        metavar="MINUTES",
        help="每隔 N 分钟再看一轮。省略则只跑一轮。",
    )
    args = p.parse_args(argv)
    if args.every and _already_running():
        print("已有清理进程在跑。", file=sys.stderr)
        return 1
    tenant = os.environ.get("AZURE_AD_TENANT_ID")
    client = os.environ.get("AZURE_AD_CLIENT_ID")
    user = os.environ.get("MAILBOX_USER", "me")
    if not tenant or not client:
        print("缺 AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID", file=sys.stderr)
        return 2

    def _round() -> dict[str, int]:
        ensure_trust_store()
        last: Exception | None = None
        for attempt in range(3):
            try:
                token = get_token(tenant, client)
                break
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
                last = exc
                LOG.warning("登录失败，15 秒后重试（%d/3）", attempt + 1)
                time.sleep(15)
        else:
            raise last  # type: ignore[misc]
        return sweep_once(token=token, user_id=user, dry_run=args.dry_run)

    if not args.every:
        stats = _round()
        print(
            f"seen={stats['seen']} deleted={stats['deleted']} "
            f"kept={stats['kept']} skipped={stats['skipped']}"
        )
        return 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        while True:
            try:
                stats = _round()
                print(
                    f"seen={stats['seen']} deleted={stats['deleted']} "
                    f"kept={stats['kept']} skipped={stats['skipped']}",
                    flush=True,
                )
            except Exception:
                LOG.exception("本轮清理失败，两小时后再试")
            time.sleep(max(args.every, 1) * 60)
    finally:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
