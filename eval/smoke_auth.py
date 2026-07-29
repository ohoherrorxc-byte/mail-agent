"""
smoke_auth.py — 验证 Azure AD auth 能否拿到 token，并调用 Graph /me 验 token 有效。

auth 模式自动选择：
  - AZURE_AD_CLIENT_SECRET 非空 → client_credentials flow（service principal，不需 user 登录）
  - 否则                       → device code flow（user 登录，token 缓存到磁盘）

跑法：
  python3 eval/smoke_auth.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 触发 .env loader
from agent.outlook_drafts import _env_loader
_env_loader.load_env(verbose=True)

import os

# 复用 outlook_drafts 的 get_token
from agent.outlook_drafts import get_token


def main() -> int:
    tenant_id = os.environ.get("AZURE_AD_TENANT_ID")
    client_id = os.environ.get("AZURE_AD_CLIENT_ID")
    secret    = os.environ.get("AZURE_AD_CLIENT_SECRET", "").strip()
    if not tenant_id or not client_id:
        print("[ERR] AZURE_AD_TENANT_ID / AZURE_AD_CLIENT_ID 未配置", file=sys.stderr)
        return 2

    print(f"tenant_id:  {tenant_id}")
    print(f"client_id:  {client_id[:8]}...")
    print(f"secret set: {bool(secret)}  →  "
          f"{'client_credentials flow' if secret else 'device code flow'}")
    print("=" * 60)

    token = get_token(tenant_id, client_id)
    if not token:
        print("[ERR] 未拿到 token")
        return 1
    print(f"[OK] 拿到 access_token（{len(token)} 字符）")
    print(f"     前 12 位: {token[:12]}...")

    # 验 token 有效：调 GET /me（或 /organization 看 service principal 能看到的）
    import requests
    print("=" * 60)
    print("验证 token 有效：调 Graph /me ...")
    r = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code == 200:
        me = r.json()
        print(f"[OK] /me 200 → displayName={me.get('displayName')!r}  "
              f"userPrincipalName={me.get('userPrincipalName')!r}")
    else:
        print(f"[WARN] /me {r.status_code}: {r.text[:200]}")
        # service principal 调 /me 会 403，但 token 本身有效
        # 再调 /organization 试一下
        r2 = requests.get(
            "https://graph.microsoft.com/v1.0/organization",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        print(f"     /organization {r2.status_code}: {r2.text[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
