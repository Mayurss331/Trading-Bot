from __future__ import annotations

import ast
import inspect
import re
import types
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from backend.db.models import CustomStrategy
from strategies.base import StrategyContext, StrategyMeta, base_frame, finalize
from strategies.registry import METAS, get_strategy, normalize_strategy_id


SLUG_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
BLOCKED_IMPORT_ROOTS = {
    "os",
    "sys",
    "subprocess",
    "pathlib",
    "shutil",
    "socket",
    "requests",
    "httpx",
    "urllib",
    "importlib",
    "builtins",
}


@dataclass(frozen=True)
class LoadedStrategy:
    id: str
    title: str
    description: str
    version: int | None
    custom_strategy_id: int | None
    code_snapshot: str | None
    analyze: Callable[[pd.DataFrame, StrategyContext], dict]


def validate_slug(slug: str) -> str:
    value = (slug or "").strip()
    if not SLUG_RE.match(value):
        raise ValueError("Slug must be 3-64 characters and contain only letters, numbers, and underscores.")
    return value


def _blocked_imports(code: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in BLOCKED_IMPORT_ROOTS:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in BLOCKED_IMPORT_ROOTS:
                found.append(node.module or "")
    return sorted(set(found))


def strategy_code_warnings(code: str) -> list[str]:
    warnings: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return warnings

    compact = re.sub(r"\s+", "", code)
    if ".shift(-" in compact:
        warnings.append("uses negative shift; this can read future candles")
    if ".bfill(" in compact or "method='bfill'" in compact or 'method="bfill"' in compact:
        warnings.append("uses backward fill; this can leak future values")
    if ".resample(" in compact:
        warnings.append("uses resample; confirm higher-timeframe candles are closed before use")
    if re.search(r"\.iloc\[[^\]]*\+\s*\d+", code):
        warnings.append("uses forward iloc indexing; confirm it does not read future rows")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "rolling":
                for kw in node.keywords:
                    if kw.arg == "center" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        warnings.append("uses rolling(center=True); centered windows include future data")
            if node.func.attr in {"idxmax", "idxmin"}:
                warnings.append(f"uses {node.func.attr}(); confirm it is not applied to full future ranges")
    return sorted(set(warnings))


def _normalize_custom_result(result: dict, meta: StrategyMeta, bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    if not isinstance(result, dict):
        raise ValueError("Strategy analyze() must return a dict.")
    if "frame" in result and "meta" in result:
        return result
    frame = result.get("frame")
    if frame is None:
        frame = result.get("signals")
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Strategy result must include a pandas DataFrame under 'frame' or 'signals'.")
    for col, default in {
        "entry_side": 0,
        "exit_long": False,
        "exit_short": False,
        "score": 0,
        "reason": meta.description,
    }.items():
        if col not in frame.columns:
            frame[col] = default
    return finalize(meta, frame, ctx)


def compile_custom_strategy(row: CustomStrategy) -> LoadedStrategy:
    slug = validate_slug(row.slug)
    code = row.code or ""
    if not code.strip():
        raise ValueError("Strategy code is empty.")
    blocked = _blocked_imports(code)
    if blocked:
        raise ValueError(f"Blocked imports in strategy code: {', '.join(blocked)}")

    module = types.ModuleType(f"db_custom_strategy_{row.id}_{row.version}")
    module.__dict__.update(
        {
            "__name__": module.__name__,
            "pd": pd,
            "StrategyMeta": StrategyMeta,
            "StrategyContext": StrategyContext,
            "base_frame": base_frame,
            "finalize": finalize,
        }
    )
    exec(compile(code, f"<custom_strategy:{slug}>", "exec"), module.__dict__)
    analyze = module.__dict__.get("analyze")
    if not callable(analyze):
        raise ValueError("Custom strategy must define analyze(bars, ctx).")
    sig = inspect.signature(analyze)
    if len(sig.parameters) < 2:
        raise ValueError("analyze must accept at least two parameters: bars and ctx.")

    meta = module.__dict__.get("META")
    if not isinstance(meta, StrategyMeta):
        meta = StrategyMeta(
            id=slug,
            name=row.title,
            description=row.description or f"Custom strategy: {row.title}",
            chart_label=row.title,
            score_label="Score",
        )

    def wrapped(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
        result = analyze(bars, ctx)
        return _normalize_custom_result(result, meta, bars, ctx)

    return LoadedStrategy(
        id=slug,
        title=row.title,
        description=row.description or meta.description,
        version=row.version,
        custom_strategy_id=row.id,
        code_snapshot=code,
        analyze=wrapped,
    )


def load_builtin_strategy(strategy_id: str) -> LoadedStrategy:
    normalized = normalize_strategy_id(strategy_id)
    meta = METAS[normalized]
    return LoadedStrategy(
        id=meta.id,
        title=meta.name,
        description=meta.description,
        version=None,
        custom_strategy_id=None,
        code_snapshot=None,
        analyze=get_strategy(normalized),
    )
