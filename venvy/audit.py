"""Audit trail."""
import logging


def audit_log(username: str, action: str, detail: str = "") -> None:
    """Lightweight audit trail — logs who did what. No dedicated table; these are
    plain INFO log lines an operator can grep/ship to their own log aggregator."""
    suffix = f" — {detail}" if detail else ""
    logging.info(f"AUDIT: user={username!r} action={action}{suffix}")
