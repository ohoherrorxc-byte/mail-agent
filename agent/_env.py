"""
_env.py — 启动时从 .env.example + .env 加载环境变量。

约定（参考 .gitignore）：
  .env.example   — 占位 / 真实值（git 忽略，公共配置）
  .env           — 真实值（git 忽略，私有 secret）

加载顺序：先 .env.example（不覆盖已有 env）→ 再 .env（不覆盖已有 env）。
两个文件都不存在 → 不报错，让 os.environ 维持原样（CI / 容器环境用）。
效果：.env.example 提供默认，.env 覆盖（典型用法：.env 只放 AZURE_AD_CLIENT_SECRET）。
"""
from __future__ import annotations

import logging
from pathlib import Path

LOG = logging.getLogger("agent._env")

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

_loaded: list[Path] = []
_already_loaded: bool = False


def load_env(verbose: bool = False) -> list[Path]:
    """从 .env.example → .env 顺序加载（不覆盖已有 env）。

    Returns: 实际加载的文件列表（按顺序）。
    注意：重复调用只加载一次。
    """
    global _already_loaded
    if _already_loaded:
        return list(_loaded)

    try:
        from dotenv import load_dotenv
    except ImportError:
        if verbose:
            LOG.warning("python-dotenv 未安装，跳过 .env 加载")
        _already_loaded = True
        return []

    # 先 .env.example（兜底缺省，override=False 只填缺失），
    # 再 .env（真实值，override=True 永远优先）。
    # 这样 .env 里有但 .env.example 没有的（如 AZURE_AD_CLIENT_SECRET）能用，
    # .env 里没但 .env.example 有的也能补上，.env 里的值永远赢。
    for path in (ENV_EXAMPLE, ENV_FILE):
        if path.exists():
            load_dotenv(path, override=(path == ENV_FILE))
            _loaded.append(path)
            if verbose:
                LOG.info("loaded env from %s (override=%s)",
                         path.name, path == ENV_FILE)

    _already_loaded = True
    return list(_loaded)
