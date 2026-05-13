from __future__ import annotations

import base64
import os
from datetime import datetime

import httpx

_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


def send_report_email(
    to_emails: list[str],
    paper_pdf: bytes | None,
    real_pdf: bytes | None,
    paper_count: int,
    real_count: int,
    generated_at: datetime,
) -> None:
    """Send trade report email with PDF attachments via Brevo HTTP API.

    Raises RuntimeError if BREVO_API_KEY or REPORT_EMAIL_FROM are not set,
    or if the Brevo API returns a non-201 response.
    """
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("REPORT_EMAIL_FROM", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY environment variable is not set.")
    if not from_email:
        raise RuntimeError("REPORT_EMAIL_FROM environment variable is not set.")

    date_str = generated_at.strftime("%Y-%m-%d")

    body_lines = [
        "CoinDCX Bot — Trade Report",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "SUMMARY",
        "-------",
        f"Paper trades:  {paper_count} trade(s)" + ("  (PDF attached)" if paper_pdf else "  — No trades recorded"),
        f"Real trades:   {real_count} trade(s)" + ("  (PDF attached)" if real_pdf else "  — No trades recorded"),
        "",
        "See the attached PDF report(s) for full trade-by-trade detail.",
        "",
        "—",
        "CoinDCX Bot Dashboard",
    ]

    attachments = []
    if paper_pdf:
        attachments.append({
            "content": base64.b64encode(paper_pdf).decode(),
            "name": f"paper_trades_{date_str}.pdf",
        })
    if real_pdf:
        attachments.append({
            "content": base64.b64encode(real_pdf).decode(),
            "name": f"real_trades_{date_str}.pdf",
        })

    payload: dict = {
        "sender": {"email": from_email},
        "to": [{"email": addr} for addr in to_emails],
        "subject": f"CoinDCX Bot Trade Report — {date_str}",
        "textContent": "\n".join(body_lines),
    }
    if attachments:
        payload["attachment"] = attachments

    response = httpx.post(
        _BREVO_ENDPOINT,
        json=payload,
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if response.status_code != 201:
        raise RuntimeError(f"Brevo API error {response.status_code}: {response.text}")

    data = response.json()
    if not data.get("messageId"):
        raise RuntimeError(f"Brevo returned unexpected response: {data}")
