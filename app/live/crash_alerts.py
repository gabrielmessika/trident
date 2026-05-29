from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
import subprocess
import time
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)


def notify_crash(
    *,
    service_name: str,
    exc: BaseException,
    state_path: str | Path | None = None,
) -> bool:
    """Best-effort crash email notification.

    The alert is disabled unless TRIDENT_CRASH_ALERT_EMAIL_TO is configured.
    SMTP is preferred; sendmail is supported as a local-server fallback.
    """

    recipient = os.getenv("TRIDENT_CRASH_ALERT_EMAIL_TO", "").strip()
    if not recipient:
        return False
    state_file = Path(
        state_path
        or os.getenv("TRIDENT_CRASH_ALERT_STATE_PATH", "runtime/trident/crash_alert_state.json")
    )
    cooldown_seconds = _float_env("TRIDENT_CRASH_ALERT_COOLDOWN_SECONDS", 300.0)
    if _cooldown_active(state_file, service_name, cooldown_seconds):
        logger.warning("Crash alert suppressed by cooldown for %s", service_name)
        return False

    message = _build_message(service_name=service_name, exc=exc, recipient=recipient)
    sent = _send_message(message)
    if sent:
        _record_alert(state_file, service_name)
    return sent


def _build_message(
    *,
    service_name: str,
    exc: BaseException,
    recipient: str,
) -> EmailMessage:
    hostname = socket.gethostname()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sender = os.getenv("TRIDENT_CRASH_ALERT_EMAIL_FROM", "").strip() or f"trident@{hostname}"
    subject = f"[TRIDENT] crash {service_name} on {hostname}"
    body = "\n".join(
        [
            f"service={service_name}",
            f"host={hostname}",
            f"timestamp={now}",
            f"exception={type(exc).__name__}: {exc}",
            "",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return message


def _send_message(message: EmailMessage) -> bool:
    smtp_host = os.getenv("TRIDENT_CRASH_ALERT_SMTP_HOST", "").strip()
    if smtp_host:
        return _send_via_smtp(message, smtp_host=smtp_host)
    sendmail_path = os.getenv("TRIDENT_CRASH_ALERT_SENDMAIL_PATH", "").strip()
    if sendmail_path:
        return _send_via_sendmail(message, sendmail_path=sendmail_path)
    logger.warning(
        "Crash alert recipient configured, but neither SMTP nor sendmail is configured"
    )
    return False


def _send_via_smtp(message: EmailMessage, *, smtp_host: str) -> bool:
    port = int(_float_env("TRIDENT_CRASH_ALERT_SMTP_PORT", 587.0))
    use_tls = os.getenv("TRIDENT_CRASH_ALERT_SMTP_TLS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    username = os.getenv("TRIDENT_CRASH_ALERT_SMTP_USER", "").strip()
    password = os.getenv("TRIDENT_CRASH_ALERT_SMTP_PASSWORD", "")
    try:
        with smtplib.SMTP(smtp_host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if username or password:
                smtp.login(username, password)
            smtp.send_message(message)
        return True
    except Exception:
        logger.exception("Crash alert SMTP send failed")
        return False


def _send_via_sendmail(message: EmailMessage, *, sendmail_path: str) -> bool:
    try:
        subprocess.run(
            [sendmail_path, "-t"],
            input=message.as_bytes(),
            check=True,
            timeout=10,
        )
        return True
    except Exception:
        logger.exception("Crash alert sendmail send failed")
        return False


def _cooldown_active(path: Path, service_name: str, cooldown_seconds: float) -> bool:
    if cooldown_seconds <= 0 or not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    try:
        last_sent = float(payload.get(service_name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return (time.time() - last_sent) < cooldown_seconds


def _record_alert(path: Path, service_name: str) -> None:
    payload: dict[str, float] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = {
                    str(key): float(value)
                    for key, value in loaded.items()
                    if isinstance(value, (int, float))
                }
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            payload = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    payload[service_name] = time.time()
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
