from __future__ import annotations

import os
from datetime import datetime


def send_report_email(
    to_email: str,
    paper_pdf: bytes | None,
    real_pdf: bytes | None,
    paper_count: int,
    real_count: int,
    generated_at: datetime,
) -> None:
    """Send trade report email with PDF attachments via Resend.

    Raises RuntimeError if RESEND_API_KEY or REPORT_EMAIL_FROM are not set,
    or if the Resend API call fails.
    """
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    from_email = os.getenv("REPORT_EMAIL_FROM", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY environment variable is not set.")
    if not from_email:
        raise RuntimeError("REPORT_EMAIL_FROM environment variable is not set.")

    import resend

    resend.api_key = api_key

    date_str = generated_at.strftime("%Y-%m-%d")
    subject = f"CoinDCX Bot Trade Report — {date_str}"

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
    body = "\n".join(body_lines)

    attachments = []
    if paper_pdf:
        attachments.append({
            "filename": f"paper_trades_{date_str}.pdf",
            "content": list(paper_pdf),
        })
    if real_pdf:
        attachments.append({
            "filename": f"real_trades_{date_str}.pdf",
            "content": list(real_pdf),
        })

    params: dict = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if attachments:
        params["attachments"] = attachments

    response = resend.Emails.send(params)

    if not response.get("id"):
        raise RuntimeError(f"Resend returned unexpected response: {response}")
