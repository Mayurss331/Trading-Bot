from __future__ import annotations

import json
import os
from typing import Any, Callable

import httpx

from .config import BacktestConfig


TRADE_VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "approve": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approve", "confidence", "reason", "risks"],
}


TradeVerifier = Callable[[dict[str, Any]], dict[str, Any]]


def _timeout(default: float = 20.0) -> float:
    try:
        return max(3.0, min(float(os.getenv("OPENAI_BACKTEST_TIMEOUT", default)), 60.0))
    except (TypeError, ValueError):
        return default


def _base_url() -> str:
    return os.getenv("OPENAI_BACKTEST_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _extract_message_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI response did not include choices.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("OpenAI response did not include a message.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    raise ValueError("OpenAI response message did not include text content.")


def _parse_decision(content: str) -> dict[str, Any]:
    try:
        decision = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        decision = json.loads(content[start : end + 1])
    if not isinstance(decision, dict):
        raise ValueError("AI verification response was not a JSON object.")
    return decision


def _fail(reason: str, model: str) -> dict[str, Any]:
    return {
        "ok": False,
        "approved": False,
        "confidence": None,
        "reason": reason,
        "risks": [],
        "model": model,
    }


def _normalise_decision(decision: dict[str, Any], cfg: BacktestConfig, model: str) -> dict[str, Any]:
    try:
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 100.0))
    approve = bool(decision.get("approve"))
    reason = str(decision.get("reason") or "No AI reason returned.").strip()
    risks_raw = decision.get("risks")
    risks = [str(item).strip() for item in risks_raw if str(item).strip()] if isinstance(risks_raw, list) else []
    return {
        "ok": True,
        "approved": approve and confidence >= cfg.ai_min_confidence,
        "confidence": confidence,
        "min_confidence": cfg.ai_min_confidence,
        "reason": reason[:600],
        "risks": risks[:8],
        "model": model,
    }


def verify_trade_with_openai(payload: dict[str, Any], cfg: BacktestConfig) -> dict[str, Any]:
    cfg = cfg.normalized()
    model = os.getenv("OPENAI_BACKTEST_MODEL", cfg.ai_model).strip() or cfg.ai_model
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return _fail("OPENAI_API_KEY is not set.", model)

    system_prompt = (
        "You are an AI verification gate for a historical crypto strategy backtest. "
        "Use only the supplied OHLCV candles, indicators, and proposed position details. "
        "Score whether the candidate entry is coherent with trend, momentum, volatility, "
        "reward/risk, stop placement, and recent market structure. Be conservative in chop, "
        "late entries, thin context, or conflicting signals. Return JSON only."
    )
    user_prompt = (
        "Evaluate this candidate trade. confidence must be 0-100. "
        f"approve should be true only when confidence is at least {cfg.ai_min_confidence:.1f} "
        "and the trade setup is acceptable.\n\n"
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_verification",
                "strict": True,
                "schema": TRADE_VERIFICATION_SCHEMA,
            },
        },
        "max_completion_tokens": 350,
    }

    url = f"{_base_url()}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.post(url, headers=headers, json=body)
            if response.status_code >= 400:
                message = response.text[:500]
                if "json_schema" in message or "response_format" in message:
                    fallback = dict(body)
                    fallback["response_format"] = {"type": "json_object"}
                    response = client.post(url, headers=headers, json=fallback)
            if response.status_code >= 400:
                return _fail(f"OpenAI API error {response.status_code}: {response.text[:300]}", model)
            content = _extract_message_content(response.json())
            decision = _parse_decision(content)
            return _normalise_decision(decision, cfg, model)
    except Exception as exc:
        return _fail(f"AI verification failed: {exc}", model)


def make_openai_trade_verifier(cfg: BacktestConfig) -> TradeVerifier:
    cfg = cfg.normalized()

    def verify(payload: dict[str, Any]) -> dict[str, Any]:
        return verify_trade_with_openai(payload, cfg)

    return verify
