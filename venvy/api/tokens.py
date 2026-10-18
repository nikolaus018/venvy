"""API tokens: long-lived credentials for scripts and CI (`Authorization: Bearer venvy_...`).

A token acts as the user who made it, with that user's role and app access;
a read-only token can only make GET requests. Tokens cannot create more
tokens, change the password or touch two-factor settings."""
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from venvy.audit import audit_log
from venvy.auth import _hash_token, check_ip_allowlist, get_session_user, new_api_token, rate_limit_mutations
from venvy.config import utc_now
from venvy.db import get_session
from venvy.models import ApiToken, User

router = APIRouter(
    prefix="/api", tags=["tokens"], dependencies=[Depends(check_ip_allowlist)]
)

MAX_TOKENS_PER_USER = 20


class TokenReq(BaseModel):
    name: str
    scope: str = "full"                   # full | read
    expires_days: Optional[int] = None    # None: never


def _json(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "scope": t.scope,
            "created_at": t.created_at.isoformat(),
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None}


@router.get("/tokens")
def list_tokens(session: Session = Depends(get_session), user: User = Depends(get_session_user)):
    rows = session.exec(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id.desc())).all()
    return [_json(t) for t in rows]


@router.post("/tokens")
def create_token(req: TokenReq, session: Session = Depends(get_session), user: User = Depends(get_session_user),
                 _rl: None = Depends(rate_limit_mutations)):
    """Make a token. The token itself is in this response and nowhere else — Venvy keeps only its hash."""
    name = req.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "A token needs a name of up to 60 characters.")
    if req.scope not in ("full", "read"):
        raise HTTPException(400, "scope must be full or read.")
    if req.expires_days is not None and not (1 <= req.expires_days <= 3650):
        raise HTTPException(400, "expires_days must be between 1 and 3650.")
    count = len(session.exec(select(ApiToken).where(ApiToken.user_id == user.id)).all())
    if count >= MAX_TOKENS_PER_USER:
        raise HTTPException(400, f"You can have {MAX_TOKENS_PER_USER} tokens at most; delete one first.")
    raw = new_api_token()
    row = ApiToken(user_id=user.id, name=name, token_hash=_hash_token(raw), prefix=raw[:12], scope=req.scope,
                   expires_at=utc_now() + datetime.timedelta(days=req.expires_days) if req.expires_days else None)
    session.add(row)
    session.commit()
    session.refresh(row)
    audit_log(user.username, "token_create", f"'{name}' ({req.scope})")
    return {**_json(row), "token": raw}


@router.delete("/tokens/{token_id}")
def delete_token(token_id: int, session: Session = Depends(get_session), user: User = Depends(get_session_user)):
    row = session.get(ApiToken, token_id)
    if not row or (row.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    session.delete(row)
    session.commit()
    audit_log(user.username, "token_delete", f"'{row.name}'")
    return {"ok": True}
