from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    config_path = path or DEFAULT_CONFIG_PATH
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    art = cfg.setdefault("artwork", {})
    if "cache_dir" in art:
        art["cache_dir"] = str(_expand_path(str(art["cache_dir"])))

    cfg["_root"] = str(ROOT)
    cfg["_assets"] = str(ROOT / "assets")
    return cfg
