"""
settings.py — маленький файл настроек рядом с игрой (settings.json): ник и последний адрес.
"""

from __future__ import annotations

import json
import os
import sys

DEFAULTS = {"name": "", "last_addr": ""}


def app_dir() -> str:
    """Папка с игрой (для .exe от PyInstaller — папка рядом с exe, а не временная)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _path() -> str:
    return os.path.join(app_dir(), "settings.json")


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key in DEFAULTS:
                if isinstance(raw.get(key), str):
                    data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump({k: data.get(k, v) for k, v in DEFAULTS.items()},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass
