from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AlertMailer:
    def __init__(self) -> None:
        self.to_addr = os.environ["ALERT_TO"]
        self.from_addr = os.environ.get("ALERT_FROM", self.to_addr)
        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_tls = _env_bool("SMTP_USE_TLS", True)

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = self.to_addr
        message.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(message)
