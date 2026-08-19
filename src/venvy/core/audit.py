"""Lightweight audit trail.

Audit entries are plain ``INFO`` log lines rather than a database table, so an
operator can grep them or ship them to an existing log aggregator without
Venvy owning yet another retention policy.
"""

from __future__ import annotations

import logging

__all__ = ["audit_log"]

logger = logging.getLogger("venvy.audit")


def audit_log(username: str, action: str, detail: str = "") -> None:
    """Record that *username* performed *action*, optionally with *detail*."""
    suffix = f" — {detail}" if detail else ""
    logger.info("AUDIT: user=%r action=%s%s", username, action, suffix)
