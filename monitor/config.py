from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MonitorConfig:
    alert_to: str
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_alerts_enabled: bool = False

    @classmethod
    def load(cls) -> MonitorConfig:
        path = Path(os.environ.get("CONFIG_PATH", "/config/monitor-config.json"))
        if not path.exists():
            raise FileNotFoundError(
                f"Monitor config not found at {path}. Run ./start.sh on the host first."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        alert_to = data["alert_to"].strip()
        smtp_user = (data.get("smtp_user") or alert_to).strip()
        password = data.get("smtp_password", "")
        email_enabled = bool(data.get("email_alerts_enabled", bool(password)))
        return cls(
            alert_to=alert_to,
            smtp_host=data.get("smtp_host", "smtp.gmail.com"),
            smtp_port=int(data.get("smtp_port", 587)),
            smtp_user=smtp_user,
            smtp_password=password,
            smtp_use_tls=bool(data.get("smtp_use_tls", True)),
            email_alerts_enabled=email_enabled and bool(password),
        )

    @property
    def alert_from(self) -> str:
        return self.smtp_user or self.alert_to
