from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import docker
from docker.errors import NotFound

from monitor.alerter import AlertMailer

logger = logging.getLogger(__name__)

REGISTER_REQUEST_RE = re.compile(
    r"Request:\s+POST\s+\S+/api/v1/users/send/register-code\b",
    re.IGNORECASE,
)
REGISTER_CODE_RE = re.compile(
    r"(?:Sending register verify code for|Generated register verify code for debug)\s+"
    r"([^\s:]+)\s*:?\s*(\d+)?",
    re.IGNORECASE,
)
SEND_EMAIL_RE = re.compile(
    r"send email to\s+([^\s,]+),\s*title:\s*Your TDengine IDMP account verification code",
    re.IGNORECASE,
)
SEND_SUCCESS_RE = re.compile(r"send msg success detailId:\s*(\d+)", re.IGNORECASE)
SEND_FAILED_RE = re.compile(
    r"send msg failed(?:\s+cause by SendMsgException:)?",
    re.IGNORECASE,
)


@dataclass
class PendingVerification:
    email: str
    code: str | None
    requested_at: float
    smtp_attempted: bool = False
    alerted: bool = False
    trace_ids: set[str] = field(default_factory=set)


class VerificationMonitor:
    def __init__(self, on_event: Callable[[str], None] | None = None) -> None:
        self.container_name = os.environ.get(
            "IDMP_CONTAINER_NAME", "idmp-tsdb-tdgpt-idmp"
        )
        self.timeout_seconds = int(os.environ.get("VERIFY_EMAIL_TIMEOUT_SECONDS", "90"))
        self.probe_interval = int(os.environ.get("PROBE_INTERVAL_SECONDS", "0"))
        self.idmp_base_url = os.environ.get(
            "IDMP_BASE_URL", "http://host.docker.internal:6042"
        ).rstrip("/")
        self.on_event = on_event or (lambda _msg: None)
        self.alerter = AlertMailer()
        self._pending: dict[str, PendingVerification] = {}
        self._lock = threading.Lock()
        self._recent_events: dict[str, float] = {}
        self._stop = threading.Event()
        self._stats = {
            "events_seen": 0,
            "successes": 0,
            "failures": 0,
            "timeouts": 0,
            "last_event_at": None,
        }

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "pending_count": len(self._pending),
            }

    def stop(self) -> None:
        self._stop.set()

    def _record(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + amount
            self._stats["last_event_at"] = datetime.now(timezone.utc).isoformat()

    def _alert(self, pending: PendingVerification, reason: str) -> None:
        if pending.alerted:
            return
        pending.alerted = True
        subject = f"[IDMP] Verification email not delivered to {pending.email}"
        body = (
            "IDMP register-step verification email monitoring detected a problem.\n\n"
            f"Recipient: {pending.email}\n"
            f"Reason: {reason}\n"
            f"Requested at (UTC): "
            f"{datetime.fromtimestamp(pending.requested_at, tz=timezone.utc).isoformat()}\n"
            f"Generated code (from IDMP logs): {pending.code or 'unknown'}\n"
            f"SMTP attempted: {'yes' if pending.smtp_attempted else 'no'}\n"
            f"IDMP container: {self.container_name}\n\n"
            "What this means:\n"
            "- The user clicked 'Send verification code' on step 1 of IDMP setup.\n"
            "- IDMP accepted the request, but the outbound email was not confirmed.\n"
            "- The user may never receive their verification code.\n\n"
            "Suggested checks:\n"
            "1. IDMP System Settings -> Email configuration\n"
            "2. POST /api/v1/system/email/default-connectivity from the IDMP UI\n"
            "3. docker logs for the IDMP container around the timestamp above\n"
        )
        self.alerter.send(subject, body)
        self.on_event(f"alert sent for {pending.email}: {reason}")

    def _dedupe(self, key: str, window_seconds: float = 2.0) -> bool:
        now = time.time()
        with self._lock:
            expired = [k for k, ts in self._recent_events.items() if now - ts > window_seconds]
            for k in expired:
                del self._recent_events[k]
            if key in self._recent_events:
                return True
            self._recent_events[key] = now
        return False

    def _start_pending(self, email: str, code: str | None = None) -> None:
        email = email.lower()
        if self._dedupe(f"pending:{email}:{code}"):
            return
        with self._lock:
            self._pending[email] = PendingVerification(
                email=email,
                code=code,
                requested_at=time.time(),
            )
        self._record("events_seen")
        self.on_event(f"register verification requested for {email}")

    def _mark_smtp_attempt(self, email: str) -> None:
        email = email.lower()
        if self._dedupe(f"smtp:{email}"):
            return
        with self._lock:
            pending = self._pending.get(email)
            if pending:
                pending.smtp_attempted = True
        self.on_event(f"smtp send attempted for {email}")

    def _mark_success(self, email: str) -> None:
        email = email.lower()
        if self._dedupe(f"success:{email}"):
            return
        with self._lock:
            pending = self._pending.pop(email, None)
        if pending:
            self._record("successes")
            self.on_event(f"verification email confirmed for {email}")

    def _handle_failure_near(self, line: str) -> None:
        with self._lock:
            candidates = list(self._pending.values())
        if not candidates:
            return
        for pending in candidates:
            if pending.email in line.lower():
                self._record("failures")
                self._alert(
                    pending,
                    f"IDMP mail worker reported send failure: {line.strip()}",
                )
                with self._lock:
                    self._pending.pop(pending.email, None)
                return
        newest = max(candidates, key=lambda item: item.requested_at)
        if time.time() - newest.requested_at <= self.timeout_seconds:
            self._record("failures")
            self._alert(
                newest,
                f"IDMP mail worker reported send failure near pending request: {line.strip()}",
            )
            with self._lock:
                self._pending.pop(newest.email, None)

    def _expire_stale(self) -> None:
        now = time.time()
        expired: list[PendingVerification] = []
        with self._lock:
            for email, pending in list(self._pending.items()):
                if now - pending.requested_at >= self.timeout_seconds:
                    expired.append(pending)
                    self._pending.pop(email, None)
        for pending in expired:
            self._record("timeouts")
            reason = (
                "SMTP was attempted but no success confirmation appeared in IDMP logs"
                if pending.smtp_attempted
                else "No outbound verification email attempt appeared in IDMP logs"
            )
            self._alert(pending, reason)

    def _process_line(self, line: str) -> None:
        if REGISTER_REQUEST_RE.search(line):
            # Request line does not include the email; wait for the code log line.
            return

        code_match = REGISTER_CODE_RE.search(line)
        if code_match:
            email = code_match.group(1)
            code = code_match.group(2)
            self._start_pending(email, code)
            return

        email_match = SEND_EMAIL_RE.search(line)
        if email_match:
            self._mark_smtp_attempt(email_match.group(1))
            return

        if SEND_SUCCESS_RE.search(line):
            with self._lock:
                if len(self._pending) == 1:
                    email = next(iter(self._pending))
                else:
                    email = None
                    for candidate in self._pending.values():
                        if candidate.smtp_attempted:
                            email = candidate.email
                            break
            if email:
                self._mark_success(email)
            return

        if SEND_FAILED_RE.search(line):
            self._handle_failure_near(line)

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(5):
            self._expire_stale()

    def _probe_loop(self) -> None:
        if self.probe_interval <= 0:
            return
        url = f"{self.idmp_base_url}/api/v1/system/email/default-connectivity"
        while not self._stop.wait(self.probe_interval):
            try:
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    ok = resp.status == 200
            except urllib.error.URLError as exc:
                ok = False
                detail = str(exc)
            else:
                detail = "connectivity endpoint returned HTTP 200"
            if not ok:
                self.alerter.send(
                    "[IDMP] Email connectivity probe failed",
                    (
                        "The proactive IDMP email connectivity probe failed.\n\n"
                        f"URL: {url}\n"
                        f"Detail: {detail}\n"
                        f"Container watched: {self.container_name}\n"
                    ),
                )
                self.on_event("proactive connectivity probe failed")

    def run(self) -> None:
        client = docker.from_env()
        container = client.containers.get(self.container_name)
        logger.info("Streaming logs from container %s", self.container_name)

        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        watchdog.start()
        if self.probe_interval > 0:
            probe = threading.Thread(target=self._probe_loop, daemon=True)
            probe.start()

        for raw in container.logs(stream=True, follow=True, tail=100):
            if self._stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace")
            try:
                self._process_line(line)
            except Exception:
                logger.exception("Failed to process log line: %s", line[:200])
