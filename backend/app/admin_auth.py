"""Who may open the admin panel and the disease review desk.

Each admin logs in with their own phone number and password. The list lives in the ADMIN_USERS
environment variable (on Render: service -> Environment), so it survives restarts and only the
person who controls the server can change it:

    ADMIN_USERS = Sunanda:9390255655:a-long-password; Ravi:9876543210:another-password

One person per entry, separated by ";" (or new lines). Passwords must be at least 8 characters.
Removing someone's entry (or changing their password) logs them out everywhere.

ZEMLI_API_KEY still works as an emergency master key (keep it secret; it's only visible on Render).
If neither ADMIN_USERS nor ZEMLI_API_KEY is set, the panel is open: for local testing only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

API_KEY = os.getenv("ZEMLI_API_KEY", "")
SESSION_HOURS = float(os.getenv("ADMIN_SESSION_HOURS", "168"))     # 7 days
_SECRET = (os.getenv("ADMIN_TOKEN_SECRET") or API_KEY or secrets.token_hex(32)).encode()

router = APIRouter()


def _clean_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def _load_admins() -> dict:
    admins = {}
    for raw in re.split(r"[;\n]", os.getenv("ADMIN_USERS", "")):
        parts = [x.strip() for x in raw.split(":", 2)]
        if len(parts) != 3 or not all(parts):
            if raw.strip():
                print("[zemli] ADMIN_USERS: skipped an entry (use Name:phone:password)")
            continue
        name, phone, pw = parts[0], _clean_phone(parts[1]), parts[2]
        if len(phone) != 10 or len(pw) < 8:
            print(f"[zemli] ADMIN_USERS: skipped {name} (needs a 10-digit phone and a password of 8+ characters)")
            continue
        admins[phone] = {"name": name, "phone": phone, "pw": pw}
    return admins


ADMINS = _load_admins()
if ADMINS:
    print(f"[zemli] admin logins: {', '.join(a['name'] for a in ADMINS.values())}")


def open_access() -> bool:
    return not ADMINS and not API_KEY


def _sig(phone: str, exp: int, pw: str) -> str:
    return hmac.new(_SECRET, f"{phone}|{exp}|{hashlib.sha256(pw.encode()).hexdigest()}".encode(), hashlib.sha256).hexdigest()[:40]


def make_token(phone: str) -> str:
    a = ADMINS[phone]
    exp = int(time.time() + SESSION_HOURS * 3600)
    body = base64.urlsafe_b64encode(f"{phone}|{exp}".encode()).decode().rstrip("=")
    return f"adm.{body}.{_sig(phone, exp, a['pw'])}"


def admin_from_token(value: Optional[str]) -> Optional[dict]:
    if not value or not value.startswith("adm."):
        return None
    try:
        _, body, sig = value.split(".")
        phone, exp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")
        exp = int(exp)
    except Exception:
        return None
    a = ADMINS.get(phone)
    if not a or exp < time.time() or not hmac.compare_digest(sig, _sig(phone, exp, a["pw"])):
        return None
    return a


def who(key: Optional[str]) -> Optional[dict]:
    """The admin behind an X-Api-Key value (a login token or the master key), or None."""
    if open_access():
        return {"name": "Local testing", "via": "open"}
    if API_KEY and key and secrets.compare_digest(key, API_KEY):
        return {"name": "Master key", "via": "master_key"}
    a = admin_from_token(key)
    return {"name": a["name"], "phone": a["phone"], "via": "login"} if a else None


def require_admin(x_api_key: Optional[str] = Header(default=None)):
    if not who(x_api_key):
        raise HTTPException(401, "Please log in as a Zemli admin")


# ---- login, with a simple brake on password guessing
_fails: dict[str, list[float]] = {}


def _too_many(k: str, limit: int) -> bool:
    now = time.time()
    _fails[k] = [t for t in _fails.get(k, []) if now - t < 900]
    return len(_fails[k]) >= limit


class AdminLogin(BaseModel):
    phone: str
    password: str


@router.post("/api/admin/login")
def admin_login(body: AdminLogin, request: Request):
    phone = _clean_phone(body.phone)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    if _too_many("p" + phone, 5) or _too_many("i" + ip, 20):   # 5 per number, 20 per network, per 15 min
        raise HTTPException(429, "Too many wrong tries. Wait 15 minutes and try again.")
    a = ADMINS.get(phone)
    if not a or not secrets.compare_digest(a["pw"].encode(), body.password.encode()):
        for k in ("p" + phone, "i" + ip):
            _fails.setdefault(k, []).append(time.time())
        time.sleep(0.5)
        raise HTTPException(401, "Wrong phone number or password" if ADMINS else
                            "No admin logins are set up yet. Add ADMIN_USERS on the server (see APP_GUIDE.md).")
    _fails.pop("p" + phone, None)
    print(f"[zemli] admin login: {a['name']}")
    return {"token": make_token(phone), "name": a["name"]}


@router.get("/api/admin/whoami")
def whoami(x_api_key: Optional[str] = Header(default=None)):
    w = who(x_api_key)
    if not w:
        raise HTTPException(401, "Please log in as a Zemli admin")
    return {"name": w["name"], "via": w["via"], "logins_enabled": bool(ADMINS)}
