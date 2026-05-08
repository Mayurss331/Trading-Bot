"""Load live_confluence_monitor.py once and expose it as `bot`."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BOT_PATH = ROOT / "Crypto" / "live_confluence_monitor.py"

_spec = importlib.util.spec_from_file_location("live_confluence_monitor", _BOT_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Cannot load bot module from {_BOT_PATH}")

bot = importlib.util.module_from_spec(_spec)
sys.modules["live_confluence_monitor"] = bot
_spec.loader.exec_module(bot)  # type: ignore[union-attr]

__all__ = ["bot", "ROOT"]
