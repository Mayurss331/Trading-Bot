from __future__ import annotations

import base64
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
    """Send trade report email with PDF attachments via Brevo.

    Raises RuntimeError if BREVO_API_KEY or REPORT_EMAIL_FROM are not set,
    or if the Brevo API call fails.
    """
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("REPORT_EMAIL_FROM", "").strip()
    if not api_key:
        raise RuntimeError("BREVO_API_KEY environment variable is not set.")
    if not from_email:
        raise RuntimeError("REPORT_EMAIL_FROM environment variable is not set.")

    import brevo_python
    from brevo_python import TransactionalEmailsApi, SendSmtpEmail, SendSmtpEmailAttachment
    from brevo_python import ApiClient, Configuration

    config = Configuration()
    config.api_key["api-key"] = api_key
    api = TransactionalEmailsApi(ApiClient(config))

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
        attachments.append(SendSmtpEmailAttachment(
            content=base64.b64encode(paper_pdf).decode(),
            name=f"paper_trades_{date_str}.pdf",
        ))
    if real_pdf:
        attachments.append(SendSmtpEmailAttachment(
            content=base64.b64encode(real_pdf).decode(),
            name=f"real_trades_{date_str}.pdf",
        ))

    email = SendSmtpEmail(
        sender={"email": from_email},
        to=[{"email": to_email}],
        subject=subject,
        text_content=body,
        attachment=attachments if attachments else None,
    )

    response = api.send_transac_email(email)

    if not response.message_id:
        raise RuntimeError(f"Brevo returned unexpected response: {response}")
