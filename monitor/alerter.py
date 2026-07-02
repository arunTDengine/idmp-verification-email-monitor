from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from monitor.config import MonitorConfig

logger = logging.getLogger(__name__)


class AlertMailer:
    def __init__(self, config: MonitorConfig | None = None) -> None:
        self.config = config or MonitorConfig.load()
        self._lock = threading.Lock()
        self._alerts_path = Path(
            os.environ.get("ALERTS_PATH", "/config/alerts.jsonl")
        )
        self.recent_alerts: list[dict] = []

    def send(self, subject: str, body: str) -> None:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "subject": subject,
            "body": body,
            "alert_to": self.config.alert_to,
            "delivered_by_email": False,
        }

        logger.warning("ALERT: %s", subject)
        logger.info("%s", body)

        if self.config.email_alerts_enabled:
            try:
                self._send_email(subject, body)
                record["delivered_by_email"] = True
            except Exception:
                logger.exception("Failed to send alert email to %s", self.config.alert_to)
                record["delivery_error"] = "smtp_send_failed"
        else:
            logger.warning(
                "Email delivery disabled; alert stored at %s and /alerts",
                self._alerts_path,
            )

        with self._lock:
            self.recent_alerts.append(record)
            self.recent_alerts = self.recent_alerts[-100:]
            self._alerts_path.parent.mkdir(parents=True, exist_ok=True)
            with self._alerts_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

    def list_alerts(self) -> list[dict]:
        with self._lock:
            return list(self.recent_alerts)

    def _send_email(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.alert_from
        message["To"] = self.config.alert_to
        message.set_content(body)

        with smtplib.SMTP(
            self.config.smtp_host, self.config.smtp_port, timeout=30
        ) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self.config.smtp_user:
                smtp.login(self.config.smtp_user, self.config.smtp_password)
            smtp.send_message(message)
