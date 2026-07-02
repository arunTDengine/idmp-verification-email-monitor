from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from monitor.config import MonitorConfig
from monitor.log_watcher import VerificationMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("idmp-verification-monitor")


class HealthHandler(BaseHTTPRequestHandler):
    monitor: VerificationMonitor

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/alerts":
            payload = {
                "alert_to": self.monitor.config.alert_to,
                "email_alerts_enabled": self.monitor.config.email_alerts_enabled,
                "alerts": self.monitor.alerter.list_alerts(),
            }
            self._json_response(200, payload)
            return

        if self.path in {"/health", "/"}:
            payload = {
                "status": "ok",
                "service": "idmp-verification-email-monitor",
                "container": os.environ.get("IDMP_CONTAINER_NAME", "idmp-monitor-idmp"),
                "alert_to": self.monitor.config.alert_to,
                "email_alerts_enabled": self.monitor.config.email_alerts_enabled,
                "stats": self.monitor.stats,
            }
            self._json_response(200, payload)
            return

        self.send_response(404)
        self.end_headers()

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.debug("health %s", format % args)


def main() -> int:
    config = MonitorConfig.load()
    monitor = VerificationMonitor(on_event=logger.info, config=config)
    HealthHandler.monitor = monitor

    port = int(os.environ.get("MONITOR_PORT", "8088"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(
        "Monitor ready for %s (email alerts: %s) on :%s/health",
        config.alert_to,
        config.email_alerts_enabled,
        port,
    )

    def shutdown(_signum: int, _frame: object) -> None:
        logger.info("Shutting down")
        monitor.stop()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        monitor.run()
    except Exception:
        logger.exception("Monitor stopped with error")
        return 1
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
