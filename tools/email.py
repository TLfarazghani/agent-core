"""Native SMTP ``send_email`` (post-Phase-12 capability completion).

Phase 1 shipped ``send_email`` only behind the opt-in ``MCP_BASE_URL`` bridge,
so a default-registry session (the one ``Core-agent.bat`` launches) had **no**
email tool at all -- the agent truthfully replied "I can't create or send
emails". This module adds a local-first SMTP implementation so the default
registry always carries ``send_email``; when ``MCP_BASE_URL`` *is* set,
``register_networked_tools`` now skips already-registered names, so the native
handler keeps the slot (send_message remains MCP-only).

Injectable seams, same pattern as run_code's docker client / web_search's
``urlopen`` / files.py's ``root``: ``smtp_factory`` (connection maker) and
``settings`` (config reader) -- tests never touch the network or the live
environment.

Config is read at call time (never at import; settings hot-reload):

    AGENT_CORE_SMTP_HOST      SMTP server, e.g. smtp.gmail.com (required to enable)
    AGENT_CORE_SMTP_PORT      default 587; 465 selects implicit TLS (SMTP_SSL)
    AGENT_CORE_SMTP_USER      login username (usually the sender address)
    AGENT_CORE_SMTP_PASSWORD  app password -- kept OUTSIDE the repo (env only)
    AGENT_CORE_SMTP_FROM      envelope From; defaults to AGENT_CORE_SMTP_USER

Keep this module stdlib-only (smtplib + email) so it ports to the other
targets like core/ does. The browser (WebGPU/Pyodide) cannot open raw SMTP
sockets -- same documented ceiling as create_docx/create_pptx.
"""

from __future__ import annotations

import email.utils
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Callable

from .files import default_root, resolve_path

DEFAULT_TIMEOUT = 30.0

# Factory: (host, port) -> SMTP-like object supporting the context-manager
# protocol and ``send_message``. Replaced wholesale in tests.
default_smtp_factory = smtplib.SMTP


def _read_settings() -> dict[str, Any]:
    """LiveSMTP config from the environment (call-time read, hot-reloadable)."""
    host = os.environ.get("AGENT_CORE_SMTP_HOST", "").strip()
    if not host:
        return {"configured": False}
    port_raw = os.environ.get("AGENT_CORE_SMTP_PORT", "587").strip()
    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"AGENT_CORE_SMTP_PORT is not an integer: {port_raw!r}")
    user = os.environ.get("AGENT_CORE_SMTP_USER", "").strip()
    password = os.environ.get("AGENT_CORE_SMTP_PASSWORD", "")
    from_addr = os.environ.get("AGENT_CORE_SMTP_FROM", "").strip() or user
    return {
        "configured": True,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
    }


_NOT_CONFIGURED = (
    "send_email is not configured. Set AGENT_CORE_SMTP_HOST (and optionally "
    "AGENT_CORE_SMTP_PORT, AGENT_CORE_SMTP_USER, AGENT_CORE_SMTP_PASSWORD, "
    "AGENT_CORE_SMTP_FROM) in the environment, e.g. for Gmail use an App "
    "Password with smtp.gmail.com:587."
)


def _parse_addresses(raw: Any, field: str) -> list[str]:
    """Comma/semicolon-separated string -> clean address list."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw).replace(";", ",").split(",")
    addresses = [p.strip() for p in parts if p.strip()]
    bad = [a for a in addresses if "@" not in a]
    if not addresses:
        raise ValueError(f"{field} must contain at least one email address")
    if bad:
        raise ValueError(f"{field} contains an address without '@': {', '.join(bad)}")
    return addresses


def _attach_files(message: EmailMessage, attachments: Any) -> list[str]:
    """Attach files resolved under the allowed file root (files.py rules)."""
    root = default_root()
    attached: list[str] = []
    for raw in attachments or []:
        path = resolve_path(str(raw), root)
        if not path.is_file():
            raise FileNotFoundError(f"attachment not found: {path}")
        data = path.read_bytes()
        maintype, subtype = (mimetypes.guess_type(str(path))[0] or "application/octet-stream").split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        attached.append(path.name)
    return attached


def make_handler(
    smtp_factory: Callable[..., Any] | None = None,
    settings: Callable[[], dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], str]:
    """Build the ``send_email`` handler with injectable SMTP + config seams."""
    factory = smtp_factory or default_smtp_factory
    read_settings = settings or _read_settings

    def send_email(arguments: dict[str, Any]) -> str:
        cfg = read_settings()
        if not cfg.get("configured"):
            raise ValueError(_NOT_CONFIGURED)

        to = _parse_addresses(arguments["to"], "to")
        subject = arguments["subject"]
        body = arguments["body"]

        message = EmailMessage()
        message["From"] = cfg["from_addr"]
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message["Date"] = email.utils.formatdate()
        message["Message-ID"] = make_msgid()
        message.set_content(body)

        attached = _attach_files(message, arguments.get("attachments"))

        user, password = cfg["user"], cfg["password"]
        if user and not password:
            raise ValueError(
                "AGENT_CORE_SMTP_USER is set but AGENT_CORE_SMTP_PASSWORD is "
                "empty -- provide the (app) password or unset AGENT_CORE_SMTP_USER"
            )

        with factory(cfg["host"], cfg["port"]) as server:
            if user:
                server.login(user, password)
            server.send_message(message)

        detail = f"; attachments: {', '.join(attached)}" if attached else ""
        return f"email sent to {', '.join(to)} (subject: {subject!r}){detail}"

    return send_email
