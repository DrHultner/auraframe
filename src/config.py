from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def discover_root(config_path: Path | None = None) -> Path:
    """Find the project/appliance root that holds config.yaml and .env."""
    if config_path is not None:
        return config_path.expanduser().resolve().parent

    env_root = os.getenv("AURAFRAME_ROOT") or os.getenv("AURAFRAME_HOME")
    if env_root:
        return Path(env_root).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config.yaml").is_file() or (candidate / ".env").is_file():
            # Prefer directories that look like this project
            if (candidate / "config.yaml").is_file():
                return candidate

    if (PACKAGE_ROOT / "config.yaml").is_file():
        return PACKAGE_ROOT

    return cwd


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path.expanduser().resolve() if path else None
    root = discover_root(config_path)
    if config_path is None:
        config_path = root / "config.yaml"

    env_files = []
    for env_path in (root / ".env", Path.cwd() / ".env", PACKAGE_ROOT / ".env"):
        env_path = env_path.resolve()
        if env_path.is_file() and env_path not in env_files:
            env_files.append(env_path)
            load_dotenv(env_path, override=True)

    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    art = cfg.setdefault("artwork", {})
    if "cache_dir" in art:
        art["cache_dir"] = str(_expand_path(str(art["cache_dir"])))

    cfg["_root"] = str(root)
    cfg["_assets"] = str(root / "assets")
    cfg["_config_path"] = str(config_path)
    cfg["_env_files"] = [str(p) for p in env_files]
    cfg["_package_root"] = str(PACKAGE_ROOT)
    return cfg


def mask_token(token: str) -> str:
    token = token.strip()
    if not token:
        return "(empty)"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]} ({len(token)} chars)"


def token_from_env(env_name: str = "AUDD_API_TOKEN") -> str:
    raw = os.getenv(env_name) or ""
    return str(raw).strip().strip('"').strip("'")


def describe_env(root: Path) -> dict[str, Any]:
    env_path = root / ".env"
    values = dotenv_values(env_path) if env_path.is_file() else {}
    file_token = str(values.get("AUDD_API_TOKEN") or "").strip().strip('"').strip("'")
    return {
        "root": str(root),
        "env_path": str(env_path),
        "env_exists": env_path.is_file(),
        "token_in_file": bool(file_token),
        "token_in_environ": bool(token_from_env()),
        "token_mask": mask_token(token_from_env() or file_token),
    }
