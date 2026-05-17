# Ported from rag-nusuk-ai/utils/config_loader.py — file-only loader.
# Database-backed loading is stripped since this temp adapter has no DB.

import json
import logging
import os
from typing import Any

# CONFIG_DIR sits next to this file inside the rag/ subtree.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")

logger = logging.getLogger(__name__)


def _load_config_from_file(file_name: str) -> dict:
    file_path = os.path.join(CONFIG_DIR, file_name)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Config file '{file_name}' not found at {file_path}")
    _, ext = os.path.splitext(file_name)
    with open(file_path, "r", encoding="utf-8") as f:
        if ext == ".json":
            return json.load(f)
        raise ValueError(f"Unsupported config extension: {ext}")


def load_config(file_name: str, force_file: bool = False) -> dict:
    """File-only config loader. The `force_file` parameter is kept for API
    compatibility with the upstream `rag-nusuk-ai` version but is now a no-op."""
    return _load_config_from_file(file_name)


def get_setting(key: str, default: Any = None) -> Any:
    parts = key.split(".")
    if len(parts) < 2:
        return default
    try:
        config = _load_config_from_file(f"{parts[0]}.json")
        for part in parts[1:]:
            if isinstance(config, dict) and part in config:
                config = config[part]
            else:
                return default
        return config
    except (FileNotFoundError, KeyError):
        return default
