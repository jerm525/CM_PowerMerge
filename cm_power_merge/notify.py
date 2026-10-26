"""Alerts. Failures to alert are logged but never mask the run's own result."""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage

from .config import NotifyConfig
from .logging_setup import get_logger

log = get_logger("notify")


def notify(cfg: NotifyConfig, status: str, subject: str, body: str) -> None:
    """status: success | warn | fail"""
    if status not in cfg.on:
        return
    hook = os.environ.get(cfg.teams_webhook_env)
    if hook:
        try:
            payload = json.dumps({"text": f"**{subject}**\n\n{body}"}).encode()
            req = urllib.request.Request(hook, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            log.info("Teams alert sent")
        except Exception as exc:
            log.error("Teams alert failed: %s", exc)
    if cfg.smtp_host and cfg.mail_to and cfg.mail_from:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, cfg.mail_from, ", ".join(cfg.mail_to)
            msg.set_content(body)
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                s.send_message(msg)
            log.info("Email alert sent to %s", cfg.mail_to)
        except Exception as exc:
            log.error("Email alert failed: %s", exc)
