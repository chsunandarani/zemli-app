"""Zemli live-trial platform: one app for employees and farmers (OTP login), plus the admin panel.

Modules (from the Zemli app module document):
  1. Village Employee: group tabs, farmer onboarding, GPS camera (in main.py /api/scans), khata + bulk give
  2. Farmer (view only): loan & subsidy, input bills, IoT lights + disease history, payment slips
  3. Admin: map, daily photo audit, money splitter & DBT, IoT devices, employees/groups
"""
from __future__ import annotations

import hashlib
import math
import os
import random
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import (Employee, Farmer, FarmerGroup, FieldVisit, InputCredit, IotDevice, IotReading, LoginSession,
                 Notice, OtpCode, Payout, Scan, engine, get_session, utcnow)

router = APIRouter()

API_KEY = os.getenv("ZEMLI_API_KEY", "")
OTP_DEMO = os.getenv("OTP_DEMO", "1") == "1"          # no SMS provider yet: show the code in app + admin panel
SESSION_DAYS = 30
GROUPS = 10
GROUP_SIZE = 10
EMI_HARVESTS = float(os.getenv("EMI_HARVESTS", "14"))  # default EMI = loan / 14 (2 harvests a year for 7 years)
AUDIT_DISTANCE_M = float(os.getenv("AUDIT_DISTANCE_M", "500"))
MAX_PHOTO_AGE_HOURS = float(os.getenv("MAX_PHOTO_AGE_HOURS", "48"))
FIELD_RADIUS_M = float(os.getenv("FIELD_RADIUS_M", "500"))     # daily photo within this distance of a farm = verified field day
GPS_POOR_M = float(os.getenv("GPS_POOR_M", "200"))             # GPS accuracy worse than this is flagged
IST = timezone(timedelta(hours=5, minutes=30))

# Infrastructure figures from the Zemli financial model (2026 deck). Editable per farmer in the admin panel.
PACKAGES = {
    "5k": {"label": "5,000 sq. ft.", "project_cost": 520000, "subsidy": 312000, "loan_approved": 165000, "farmer_share": 43000},
    "10k": {"label": "10,000 sq. ft.", "project_cost": 880000, "subsidy": 551000, "loan_approved": 259000, "farmer_share": 70000},
}

# IoT thresholds. Rough starting values: the agronomist should tune these per crop and soil.
MOISTURE_DRY = float(os.getenv("MOISTURE_DRY", "30"))      # % below this = dry -> red + Telugu alert
TEMP_HOT = float(os.getenv("TEMP_HOT", "38"))              # deg C above this = too hot
NPK_LOW = {"nitrogen": 40.0, "phosphorus": 15.0, "potassium": 100.0}   # mg/kg below this = low
READING_STALE_MIN = 60                                     # no reading for an hour = device offline
MOISTURE_ALERT_TE = "పొలంలో తడి తగ్గింది, వెంటనే డ్రిప్ ఆన్ చేయండి."
MOISTURE_ALERT_EN = "Soil moisture is low. Switch on the drip now."


# ---------------------------------------------------------------- helpers
def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops the timezone; treat stored times as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return aware(dt).isoformat() if dt else None


def clean_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) != 10 or d[0] not in "6789":
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    return d


def hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}:{API_KEY}".encode()).hexdigest()


def mask(value: Optional[str], keep: int = 4) -> Optional[str]:
    if not value:
        return value
    return "X" * max(0, len(value) - keep) + value[-keep:]


def metres_between(lat1, lon1, lat2, lon2) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ensure_groups() -> None:
    with Session(engine) as s:
        for gid in range(1, GROUPS + 1):
            if not s.get(FarmerGroup, gid):
                s.add(FarmerGroup(id=gid, name=f"Group {gid}"))
        s.commit()


def core():
    from . import main   # imported late to avoid a circular import
    return main


async def push_farmer(farmer_id: int, message: dict) -> None:
    await core().hub.to_employee(f"farmer:{farmer_id}", message)


def send_sms_otp(phone: str, code: str) -> None:
    """Plug an SMS provider in here (for example MSG91 or Twilio, with a DLT-registered template).
    Until then the code is printed in the server terminal and shown in demo mode."""
    print(f"[zemli] OTP for {phone}: {code}")


# ---------------------------------------------------------------- auth
def user_from_auth(authorization: Optional[str], roles: set[str]):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    with Session(engine) as s:
        sess = s.get(LoginSession, token)
        if not sess or aware(sess.expires) < utcnow() or sess.role not in roles:
            return None
        model = Employee if sess.role == "employee" else Farmer
        user = s.get(model, sess.user_id)
        if not user or (sess.role == "employee" and not user.active):
            return None
        return sess.role, user


def require(role: str):
    def dep(authorization: Optional[str] = Header(default=None)):
        u = user_from_auth(authorization, {role})
        if not u:
            raise HTTPException(401, "Please log in again")
        return u[1]
    return dep


def admin_only(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Missing or wrong API key")


class OtpRequest(BaseModel):
    phone: str


class OtpVerify(BaseModel):
    phone: str
    code: str


@router.post("/api/auth/request-otp")
def request_otp(body: OtpRequest, session: Session = Depends(get_session)):
    phone = clean_phone(body.phone)
    emp = session.exec(select(Employee).where(Employee.phone == phone, Employee.active == True)).first()  # noqa: E712
    farmer = None if emp else session.exec(select(Farmer).where(Farmer.phone == phone)).first()
    if not emp and not farmer:
        if not session.exec(select(Employee)).first():   # brand-new database: nobody can log in yet
            raise HTTPException(404, "No users yet. Admin: open /admin, then 'Demo & login codes' -> 'Load demo data', "
                                     "or add an employee under 'Employees & groups'.")
        raise HTTPException(404, "This number is not registered with Zemli. Ask your Zemli employee to add you.")
    code = f"{secrets.randbelow(1000000):06d}"
    row = session.get(OtpCode, phone) or OtpCode(phone=phone, code_hash="", expires=utcnow())
    row.code_hash, row.expires, row.attempts = hash_code(phone, code), utcnow() + timedelta(minutes=5), 0
    row.shown_code = code if OTP_DEMO else None
    session.add(row)
    session.commit()
    send_sms_otp(phone, code)
    out = {"sent": True}
    if OTP_DEMO:
        out["demo_otp"] = code
    return out


@router.post("/api/auth/verify")
def verify_otp(body: OtpVerify, session: Session = Depends(get_session)):
    phone = clean_phone(body.phone)
    row = session.get(OtpCode, phone)
    if not row or aware(row.expires) < utcnow():
        raise HTTPException(400, "The code has expired. Request a new one.")
    if row.attempts >= 5:
        raise HTTPException(429, "Too many wrong tries. Request a new code.")
    if row.code_hash != hash_code(phone, body.code.strip()):
        row.attempts += 1
        session.add(row)
        session.commit()
        raise HTTPException(400, "Wrong code")
    session.delete(row)
    emp = session.exec(select(Employee).where(Employee.phone == phone, Employee.active == True)).first()  # noqa: E712
    role, user = ("employee", emp) if emp else ("farmer", session.exec(select(Farmer).where(Farmer.phone == phone)).first())
    if not user:
        raise HTTPException(404, "Number not registered")
    token = secrets.token_urlsafe(32)
    session.add(LoginSession(token=token, role=role, user_id=user.id, expires=utcnow() + timedelta(days=SESSION_DAYS)))
    session.commit()
    return {"token": token, "role": role, "user_id": user.id, "name": user.name}


@router.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None), session: Session = Depends(get_session)):
    if authorization and authorization.lower().startswith("bearer "):
        row = session.get(LoginSession, authorization.split(" ", 1)[1].strip())
        if row:
            session.delete(row)
            session.commit()
    return {"ok": True}


# ---------------------------------------------------------------- shared views
def farmer_public(f: Farmer, full_bank: bool = False) -> dict:
    return {
        "id": f.id, "name": f.name, "phone": f.phone, "group_id": f.group_id, "village": f.village,
        "aadhaar": f"XXXX XXXX {f.aadhaar_last4}" if f.aadhaar_last4 else None,
        "bank_account": f.bank_account if full_bank else mask(f.bank_account),
        "ifsc": f.ifsc, "survey_no": f.survey_no, "package": f.package,
        "package_label": PACKAGES.get(f.package, {}).get("label", f.package),
        "latitude": f.latitude, "longitude": f.longitude,
        "project_cost": f.project_cost, "subsidy": f.subsidy, "subsidy_received": f.subsidy_received,
        "loan_approved": f.loan_approved, "loan_repaid": f.loan_repaid,
        "loan_remaining": max(0.0, f.loan_approved - f.loan_repaid), "farmer_share": f.farmer_share,
        "is_demo": f.is_demo,
    }


def pending_bill(session: Session, farmer_id: int) -> float:
    rows = session.exec(select(InputCredit).where(InputCredit.farmer_id == farmer_id, InputCredit.settled_in == None)).all()  # noqa: E711
    return round(sum(r.amount for r in rows), 2)


def iot_status(r: Optional[IotReading]) -> dict:
    if not r:
        return {"online": False, "light": "grey", "moisture": None, "weather": None, "nutrients": None, "reading": None}
    stale = aware(r.ts) < utcnow() - timedelta(minutes=READING_STALE_MIN)
    moisture = None if r.soil_moisture is None else ("dry" if r.soil_moisture < MOISTURE_DRY else "wet")
    weather = None if r.air_temp is None else ("hot" if r.air_temp > TEMP_HOT else "ok")
    low = [k for k, lim in NPK_LOW.items() if getattr(r, k) is not None and getattr(r, k) < lim]
    nutrients = None if all(getattr(r, k) is None for k in NPK_LOW) else ("low" if low else "ok")
    bad = moisture == "dry" or weather == "hot" or nutrients == "low"
    return {
        "online": not stale, "light": "grey" if stale else ("red" if bad else "green"),
        "moisture": moisture, "weather": weather, "nutrients": nutrients, "low_nutrients": low,
        "reading": {"ts": iso(r.ts), "soil_moisture": r.soil_moisture, "air_temp": r.air_temp, "humidity": r.humidity,
                    "nitrogen": r.nitrogen, "phosphorus": r.phosphorus, "potassium": r.potassium},
    }


def latest_reading(session: Session, farmer_id: int) -> Optional[IotReading]:
    return session.exec(select(IotReading).where(IotReading.farmer_id == farmer_id)
                        .order_by(IotReading.ts.desc()).limit(1)).first()


def payout_dict(p: Payout) -> dict:
    return {"id": p.id, "farmer_id": p.farmer_id, "buyer": p.buyer, "crop": p.crop, "quantity_kg": p.quantity_kg,
            "rate_per_kg": p.rate_per_kg, "gross": p.gross, "input_deduction": p.input_deduction,
            "emi_deduction": p.emi_deduction, "net": p.net, "status": p.status, "reference": p.reference,
            "created_at": iso(p.created_at), "paid_at": iso(p.paid_at)}


def credit_dict(c: InputCredit) -> dict:
    return {"id": c.id, "farmer_id": c.farmer_id, "item": c.item, "quantity": c.quantity, "amount": c.amount,
            "given_on": iso(c.given_on), "bulk_id": c.bulk_id, "settled": c.settled_in is not None}


# ---------------------------------------------------------------- module 1: village employee
@router.get("/api/me")
def me(authorization: Optional[str] = Header(default=None)):
    u = user_from_auth(authorization, {"employee", "farmer"})
    if not u:
        raise HTTPException(401, "Please log in again")
    role, user = u
    return {"role": role, "id": user.id, "name": user.name, "phone": user.phone,
            "scan_id": f"E{user.id}" if role == "employee" else None}


@router.get("/api/packages")
def packages():
    return PACKAGES


@router.get("/api/emp/groups")
def emp_groups(emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    out = []
    for g in session.exec(select(FarmerGroup).order_by(FarmerGroup.id)).all():
        farmers = session.exec(select(Farmer).where(Farmer.group_id == g.id).order_by(Farmer.name)).all()
        out.append({"id": g.id, "name": g.name, "village": g.village, "employee_id": g.employee_id,
                    "farmers": [farmer_public(f) | {"pending_bill": pending_bill(session, f.id)} for f in farmers]})
    return out


class FarmerIn(BaseModel):
    name: str
    phone: str
    group_id: int
    village: Optional[str] = None
    aadhaar: Optional[str] = None
    bank_account: Optional[str] = None
    ifsc: Optional[str] = None
    survey_no: Optional[str] = None
    package: str = "5k"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def validate_farmer(body: FarmerIn) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "Enter the farmer's name")
    if body.package not in PACKAGES:
        raise HTTPException(400, "Package must be 5k or 10k")
    if not 1 <= body.group_id <= GROUPS:
        raise HTTPException(400, f"Group must be 1 to {GROUPS}")
    aadhaar = re.sub(r"\D", "", body.aadhaar or "")
    if aadhaar and len(aadhaar) != 12:
        raise HTTPException(400, "Aadhaar must be 12 digits")
    acct = re.sub(r"\s", "", body.bank_account or "")
    if acct and not re.fullmatch(r"\d{9,18}", acct):
        raise HTTPException(400, "Bank account number must be 9 to 18 digits")
    ifsc = (body.ifsc or "").strip().upper()
    if ifsc and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        raise HTTPException(400, "IFSC looks wrong. Example: SBIN0001234")
    return {"name": body.name.strip(), "phone": clean_phone(body.phone), "group_id": body.group_id,
            "village": (body.village or "").strip() or None, "aadhaar_last4": aadhaar[-4:] if aadhaar else None,
            "bank_account": acct or None, "ifsc": ifsc or None, "survey_no": (body.survey_no or "").strip() or None,
            "package": body.package, "latitude": body.latitude, "longitude": body.longitude}


@router.post("/api/emp/farmers")
def onboard_farmer(body: FarmerIn, emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    data = validate_farmer(body)
    if session.exec(select(Farmer).where(Farmer.phone == data["phone"])).first():
        raise HTTPException(400, "A farmer with this phone number already exists")
    if session.exec(select(Employee).where(Employee.phone == data["phone"])).first():
        raise HTTPException(400, "This number belongs to a Zemli employee")
    count = len(session.exec(select(Farmer.id).where(Farmer.group_id == data["group_id"])).all())
    if count >= GROUP_SIZE:
        raise HTTPException(400, f"Group {data['group_id']} already has {GROUP_SIZE} farmers")
    f = Farmer(**data, **{k: PACKAGES[data["package"]][k] for k in ("project_cost", "subsidy", "loan_approved", "farmer_share")},
               created_by=emp.id)
    session.add(f)
    session.commit()
    session.refresh(f)
    return farmer_public(f)


class LocationIn(BaseModel):
    latitude: float
    longitude: float


@router.put("/api/emp/farmers/{farmer_id}/location")
def set_location(farmer_id: int, body: LocationIn, emp: Employee = Depends(require("employee")),
                 session: Session = Depends(get_session)):
    f = session.get(Farmer, farmer_id)
    if not f:
        raise HTTPException(404, "Farmer not found")
    f.latitude, f.longitude = body.latitude, body.longitude
    session.add(f)
    session.commit()
    return farmer_public(f)


class CreditIn(BaseModel):
    farmer_id: int
    item: str
    quantity: Optional[str] = None
    amount: float


class BulkCreditIn(BaseModel):
    group_id: int
    item: str
    quantity: Optional[str] = None
    total_amount: float


def split_amount(total: float, n: int) -> list[float]:
    """Equal shares in paise; the first farmers absorb the leftover paise so the sum is exact."""
    paise = round(total * 100)
    base, extra = divmod(paise, n)
    return [(base + (1 if i < extra else 0)) / 100 for i in range(n)]


@router.post("/api/emp/credits")
def give_credit(body: CreditIn, emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    if body.amount <= 0 or not body.item.strip():
        raise HTTPException(400, "Enter the item and an amount above zero")
    if not session.get(Farmer, body.farmer_id):
        raise HTTPException(404, "Farmer not found")
    c = InputCredit(farmer_id=body.farmer_id, item=body.item.strip(), quantity=body.quantity, amount=round(body.amount, 2), given_by=emp.id)
    session.add(c)
    session.commit()
    session.refresh(c)
    return credit_dict(c)


@router.post("/api/emp/credits/bulk")
def bulk_give(body: BulkCreditIn, emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    if body.total_amount <= 0 or not body.item.strip():
        raise HTTPException(400, "Enter the item and a total above zero")
    farmers = session.exec(select(Farmer).where(Farmer.group_id == body.group_id).order_by(Farmer.id)).all()
    if not farmers:
        raise HTTPException(400, "This group has no farmers yet")
    bulk = uuid.uuid4().hex[:10]
    rows = []
    for f, share in zip(farmers, split_amount(body.total_amount, len(farmers))):
        c = InputCredit(farmer_id=f.id, item=body.item.strip(), quantity=body.quantity, amount=share, given_by=emp.id, bulk_id=bulk)
        session.add(c)
        rows.append(c)
    session.commit()
    return {"bulk_id": bulk, "farmers": len(rows), "per_farmer": [credit_dict(c) for c in rows]}


@router.get("/api/emp/credits")
def recent_credits(group_id: Optional[int] = None, emp: Employee = Depends(require("employee")),
                   session: Session = Depends(get_session)):
    q = select(InputCredit, Farmer).join(Farmer, Farmer.id == InputCredit.farmer_id)
    if group_id:
        q = q.where(Farmer.group_id == group_id)
    rows = session.exec(q.order_by(InputCredit.given_on.desc()).limit(100)).all()
    return [credit_dict(c) | {"farmer_name": f.name} for c, f in rows]


# ---------------------------------------------------------------- module 2: farmer (view only)
@router.get("/api/farmer/summary")
def farmer_summary(f: Farmer = Depends(require("farmer")), session: Session = Depends(get_session)):
    credits = session.exec(select(InputCredit).where(InputCredit.farmer_id == f.id).order_by(InputCredit.given_on.desc())).all()
    scans = session.exec(select(Scan).where(Scan.farmer_id == str(f.id)).order_by(Scan.received_at.desc()).limit(30)).all()
    payouts = session.exec(select(Payout).where(Payout.farmer_id == f.id).order_by(Payout.created_at.desc())).all()
    notices = session.exec(select(Notice).where(Notice.farmer_id == f.id).order_by(Notice.created_at.desc()).limit(10)).all()
    to_dict = core().scan_to_dict
    return {
        "farmer": farmer_public(f),
        "bills": [credit_dict(c) for c in credits],
        "pending_bill": pending_bill(session, f.id),
        "iot": iot_status(latest_reading(session, f.id)),
        "diseases": [to_dict(s) for s in scans if s.status != "rejected"],
        "payouts": [payout_dict(p) for p in payouts if p.status == "paid"],
        "notices": [{"kind": n.kind, "text_te": n.text_te, "text_en": n.text_en, "created_at": iso(n.created_at)} for n in notices],
    }


@router.get("/api/farmer/iot-history")
def farmer_iot_history(hours: int = 24, f: Farmer = Depends(require("farmer")), session: Session = Depends(get_session)):
    hours = max(1, min(hours, 168))
    rows = session.exec(select(IotReading).where(IotReading.farmer_id == f.id, IotReading.ts > utcnow() - timedelta(hours=hours))
                        .order_by(IotReading.ts)).all()
    return [{"ts": iso(r.ts), "soil_moisture": r.soil_moisture, "air_temp": r.air_temp, "humidity": r.humidity,
             "nitrogen": r.nitrogen, "phosphorus": r.phosphorus, "potassium": r.potassium} for r in rows]


# ---------------------------------------------------------------- IoT (ESP32 + 4G box)
class ReadingIn(BaseModel):
    soil_moisture: Optional[float] = None
    air_temp: Optional[float] = None
    humidity: Optional[float] = None
    nitrogen: Optional[float] = None
    phosphorus: Optional[float] = None
    potassium: Optional[float] = None


@router.post("/api/iot/reading")
async def iot_reading(body: ReadingIn, x_device_id: str = Header(...), x_device_key: str = Header(...),
                      session: Session = Depends(get_session)):
    dev = session.get(IotDevice, x_device_id)
    if not dev or not secrets.compare_digest(dev.key, x_device_key):
        raise HTTPException(401, "Unknown device or wrong key")
    r = IotReading(device_id=dev.id, farmer_id=dev.farmer_id, **body.model_dump())
    dev.last_seen = utcnow()
    session.add(r)
    session.add(dev)
    alert = False
    if dev.farmer_id and r.soil_moisture is not None and r.soil_moisture < MOISTURE_DRY:
        recent = session.exec(select(Notice).where(Notice.farmer_id == dev.farmer_id, Notice.kind == "moisture",
                                                   Notice.created_at > utcnow() - timedelta(hours=6))).first()
        if not recent:   # at most one moisture alert every 6 hours
            session.add(Notice(farmer_id=dev.farmer_id, kind="moisture", text_te=MOISTURE_ALERT_TE, text_en=MOISTURE_ALERT_EN))
            alert = True
    session.commit()
    if alert:
        await push_farmer(dev.farmer_id, {"type": "notice", "kind": "moisture", "text_te": MOISTURE_ALERT_TE, "text_en": MOISTURE_ALERT_EN})
    return {"ok": True, "alert_sent": alert, "status": iot_status(r)}


# ---------------------------------------------------------------- module 3: admin panel
@router.get("/api/admin/overview", dependencies=[Depends(admin_only)])
def overview(session: Session = Depends(get_session)):
    farmers = session.exec(select(Farmer)).all()
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    scans_today = session.exec(select(Scan).where(Scan.received_at >= today)).all()
    unsettled = session.exec(select(InputCredit).where(InputCredit.settled_in == None)).all()  # noqa: E711
    devices = session.exec(select(IotDevice)).all()
    paid = session.exec(select(Payout).where(Payout.status == "paid")).all()
    return {
        "farmers": len(farmers), "employees": len(session.exec(select(Employee)).all()),
        "farmers_with_location": sum(1 for f in farmers if f.latitude is not None),
        "scans_today": len(scans_today), "needs_review": len(session.exec(select(Scan).where(Scan.status == "needs_review")).all()),
        "pending_bills": round(sum(c.amount for c in unsettled), 2),
        "devices": len(devices), "devices_online": sum(1 for d in devices if d.last_seen and aware(d.last_seen) > utcnow() - timedelta(minutes=READING_STALE_MIN)),
        "paid_to_farmers": round(sum(p.net for p in paid), 2),
        "loan_outstanding": round(sum(max(0, f.loan_approved - f.loan_repaid) for f in farmers), 2),
        "otp_demo": OTP_DEMO,
        "in_field_today": len({v.employee_id for v in session.exec(select(FieldVisit).where(FieldVisit.day == datetime.now(IST).strftime("%Y-%m-%d"))).all()}),
    }


@router.get("/api/admin/farmers", dependencies=[Depends(admin_only)])
def admin_farmers(session: Session = Depends(get_session)):
    out = []
    for f in session.exec(select(Farmer).order_by(Farmer.group_id, Farmer.name)).all():
        last_scan = session.exec(select(Scan).where(Scan.farmer_id == str(f.id)).order_by(Scan.received_at.desc()).limit(1)).first()
        out.append(farmer_public(f, full_bank=True) | {
            "pending_bill": pending_bill(session, f.id), "iot": iot_status(latest_reading(session, f.id)),
            "last_scan": iso(last_scan.received_at) if last_scan else None,
            "last_scan_result": last_scan and core().scan_to_dict(last_scan)["result"]["name_en"]})
    return out


class FarmerFinance(BaseModel):
    project_cost: Optional[float] = None
    subsidy: Optional[float] = None
    subsidy_received: Optional[float] = None
    loan_approved: Optional[float] = None
    loan_repaid: Optional[float] = None
    farmer_share: Optional[float] = None
    group_id: Optional[int] = None
    village: Optional[str] = None


@router.put("/api/admin/farmers/{farmer_id}", dependencies=[Depends(admin_only)])
def edit_farmer(farmer_id: int, body: FarmerFinance, session: Session = Depends(get_session)):
    f = session.get(Farmer, farmer_id)
    if not f:
        raise HTTPException(404, "Farmer not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(f, k, v)
    session.add(f)
    session.commit()
    return farmer_public(f, full_bank=True)


class EmployeeIn(BaseModel):
    name: str
    phone: str


@router.get("/api/admin/employees", dependencies=[Depends(admin_only)])
def list_employees(session: Session = Depends(get_session)):
    groups = session.exec(select(FarmerGroup).order_by(FarmerGroup.id)).all()
    return {"employees": [e.model_dump() for e in session.exec(select(Employee)).all()],
            "groups": [g.model_dump() for g in groups]}


@router.post("/api/admin/employees", dependencies=[Depends(admin_only)])
def add_employee(body: EmployeeIn, session: Session = Depends(get_session)):
    phone = clean_phone(body.phone)
    if session.exec(select(Employee).where(Employee.phone == phone)).first() or session.exec(select(Farmer).where(Farmer.phone == phone)).first():
        raise HTTPException(400, "This phone number is already registered")
    e = Employee(name=body.name.strip(), phone=phone)
    session.add(e)
    session.commit()
    session.refresh(e)
    return e.model_dump()


class GroupIn(BaseModel):
    employee_id: Optional[int] = None
    village: Optional[str] = None
    name: Optional[str] = None


@router.put("/api/admin/groups/{group_id}", dependencies=[Depends(admin_only)])
def edit_group(group_id: int, body: GroupIn, session: Session = Depends(get_session)):
    g = session.get(FarmerGroup, group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(g, k, v)
    session.add(g)
    session.commit()
    return g.model_dump()


@router.get("/api/admin/audit", dependencies=[Depends(admin_only)])
def photo_audit(date: Optional[str] = None, employee: Optional[str] = None, session: Session = Depends(get_session)):
    """Daily geo-tagged photo audit: every scan of the day, its GPS, and how far it was taken from the farm."""
    day = datetime.fromisoformat(date).replace(tzinfo=timezone.utc) if date else utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(Scan).where(Scan.received_at >= day, Scan.received_at < day + timedelta(days=1))
    if employee:
        q = q.where(Scan.employee_id == employee)
    emps = {f"E{e.id}": e.name for e in session.exec(select(Employee)).all()}
    out = []
    for s in session.exec(q.order_by(Scan.received_at.desc())).all():
        f = session.get(Farmer, int(s.farmer_id)) if (s.farmer_id or "").isdigit() else None
        dist = metres_between(s.latitude, s.longitude, f.latitude, f.longitude) if f else None
        flags = []
        if s.latitude is None:
            flags.append("No GPS")
        if dist is not None and dist > AUDIT_DISTANCE_M:
            flags.append(f"{dist/1000:.1f} km from farm")
        if not f:
            flags.append("No farmer linked")
        if (s.employee_id or "").startswith("farmer:"):
            flags = [x for x in flags if "from farm" not in x]
        if s.source == "upload":
            flags.append("Uploaded file, not live camera")
            if s.captured_at is None:
                flags.append("No date in photo")
        out.append({"id": s.id, "image_url": f"/uploads/{s.image_path}", "employee_id": s.employee_id,
                    "employee_name": emps.get(s.employee_id) or ("Farmer self-scan" if (s.employee_id or "").startswith("farmer:") else s.employee_id), "farmer": f and f.name, "farmer_id": f and f.id,
                    "crop": s.crop, "result": core().scan_to_dict(s)["result"]["name_en"], "latitude": s.latitude,
                    "longitude": s.longitude, "captured_at": iso(s.captured_at), "source": s.source or "camera", "received_at": iso(s.received_at),
                    "distance_m": None if dist is None else round(dist), "flags": flags})
    return out


class SaleLine(BaseModel):
    farmer_id: int
    gross: float
    buyer: str = "BigBasket"
    crop: Optional[str] = None
    quantity_kg: Optional[float] = None
    rate_per_kg: Optional[float] = None
    emi: Optional[float] = None   # override the default EMI for this harvest


class SaleBatch(BaseModel):
    lines: list[SaleLine]


def calculate(session: Session, line: SaleLine) -> dict:
    f = session.get(Farmer, line.farmer_id)
    if not f:
        raise HTTPException(404, f"Farmer {line.farmer_id} not found")
    gross = round(line.gross, 2)
    seed_bill = pending_bill(session, f.id)
    input_ded = min(seed_bill, gross)
    remaining_loan = max(0.0, f.loan_approved - f.loan_repaid)
    default_emi = round(f.loan_approved / EMI_HARVESTS, 2) if EMI_HARVESTS else 0
    emi = line.emi if line.emi is not None else default_emi
    emi_ded = round(max(0.0, min(emi, remaining_loan, gross - input_ded)), 2)
    return {"farmer_id": f.id, "farmer": f.name, "group_id": f.group_id, "bank_account": mask(f.bank_account), "ifsc": f.ifsc,
            "buyer": line.buyer, "crop": line.crop, "quantity_kg": line.quantity_kg, "rate_per_kg": line.rate_per_kg,
            "gross": gross, "seed_bill_pending": seed_bill, "input_deduction": round(input_ded, 2),
            "emi_deduction": emi_ded, "net": round(gross - input_ded - emi_ded, 2), "loan_remaining_after": round(remaining_loan - emi_ded, 2),
            "missing_bank": not (f.bank_account and f.ifsc)}


@router.post("/api/admin/payouts/preview", dependencies=[Depends(admin_only)])
def preview_payouts(body: SaleBatch, session: Session = Depends(get_session)):
    return [calculate(session, l) for l in body.lines if l.gross > 0]


@router.post("/api/admin/payouts", dependencies=[Depends(admin_only)])
def create_payouts(body: SaleBatch, session: Session = Depends(get_session)):
    out = []
    for line in body.lines:
        if line.gross <= 0:
            continue
        c = calculate(session, line)
        p = Payout(farmer_id=c["farmer_id"], buyer=c["buyer"], crop=c["crop"], quantity_kg=c["quantity_kg"], rate_per_kg=c["rate_per_kg"],
                   gross=c["gross"], input_deduction=c["input_deduction"], emi_deduction=c["emi_deduction"], net=c["net"])
        session.add(p)
        session.commit()
        session.refresh(p)
        out.append(payout_dict(p) | {"farmer": c["farmer"]})
    return out


@router.get("/api/admin/payouts", dependencies=[Depends(admin_only)])
def list_payouts(session: Session = Depends(get_session)):
    rows = session.exec(select(Payout, Farmer).join(Farmer, Farmer.id == Payout.farmer_id).order_by(Payout.created_at.desc()).limit(300)).all()
    return [payout_dict(p) | {"farmer": f.name, "group_id": f.group_id, "bank_account": mask(f.bank_account), "ifsc": f.ifsc} for p, f in rows]


@router.post("/api/admin/payouts/{payout_id}/pay", dependencies=[Depends(admin_only)])
async def pay(payout_id: int, session: Session = Depends(get_session)):
    """PROTOTYPE: records the payment and settles the bills and EMI. No money moves.
    For real DBT, call a licensed payout/escrow partner's API here and store its transfer reference."""
    p = session.get(Payout, payout_id)
    if not p:
        raise HTTPException(404, "Payout not found")
    if p.status == "paid":
        return payout_dict(p)
    f = session.get(Farmer, p.farmer_id)
    to_cover = p.input_deduction
    for c in session.exec(select(InputCredit).where(InputCredit.farmer_id == f.id, InputCredit.settled_in == None)  # noqa: E711
                          .order_by(InputCredit.given_on)).all():
        if to_cover <= 0.004:
            break
        if c.amount <= to_cover + 0.004:
            c.settled_in = p.id
            to_cover -= c.amount
        else:   # partly recovered: split the bill into a settled part and a still-pending part
            session.add(InputCredit(farmer_id=c.farmer_id, item=c.item, quantity=c.quantity, amount=round(c.amount - to_cover, 2),
                                    given_on=c.given_on, given_by=c.given_by, bulk_id=c.bulk_id))
            c.amount, c.settled_in, to_cover = round(to_cover, 2), p.id, 0
        session.add(c)
    f.loan_repaid = round(f.loan_repaid + p.emi_deduction, 2)
    p.status, p.paid_at, p.reference = "paid", utcnow(), f"SIM-{uuid.uuid4().hex[:8].upper()}"
    te = f"₹{p.net:,.0f} మీ బ్యాంక్ ఖాతాకు పంపబడింది. మొత్తం ఆదాయం ₹{p.gross:,.0f}, విత్తనాల బిల్లు ₹{p.input_deduction:,.0f}, రుణ EMI ₹{p.emi_deduction:,.0f}."
    en = f"₹{p.net:,.0f} sent to your bank account. Income ₹{p.gross:,.0f}, seed bill ₹{p.input_deduction:,.0f}, loan EMI ₹{p.emi_deduction:,.0f}."
    session.add(Notice(farmer_id=f.id, kind="payment", text_te=te, text_en=en))
    session.add(f)
    session.add(p)
    session.commit()
    await push_farmer(f.id, {"type": "notice", "kind": "payment", "text_te": te, "text_en": en})
    return payout_dict(p)


class DeviceIn(BaseModel):
    device_id: str
    farmer_id: Optional[int] = None


@router.get("/api/admin/devices", dependencies=[Depends(admin_only)])
def list_devices(session: Session = Depends(get_session)):
    out = []
    for d in session.exec(select(IotDevice)).all():
        f = session.get(Farmer, d.farmer_id) if d.farmer_id else None
        r = session.exec(select(IotReading).where(IotReading.device_id == d.id).order_by(IotReading.ts.desc()).limit(1)).first()
        out.append({"id": d.id, "key": d.key, "farmer_id": d.farmer_id, "farmer": f and f.name, "last_seen": iso(d.last_seen), "status": iot_status(r)})
    return out


@router.post("/api/admin/devices", dependencies=[Depends(admin_only)])
def add_device(body: DeviceIn, session: Session = Depends(get_session)):
    dev_id = body.device_id.strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{3,32}", dev_id):
        raise HTTPException(400, "Device ID: letters, numbers and dashes only")
    d = session.get(IotDevice, dev_id) or IotDevice(id=dev_id, key=secrets.token_urlsafe(16))
    d.farmer_id = body.farmer_id
    session.add(d)
    session.commit()
    return {"id": d.id, "key": d.key, "farmer_id": d.farmer_id}


@router.get("/api/admin/iot/{farmer_id}", dependencies=[Depends(admin_only)])
def farmer_readings(farmer_id: int, hours: int = 48, session: Session = Depends(get_session)):
    rows = session.exec(select(IotReading).where(IotReading.farmer_id == farmer_id, IotReading.ts > utcnow() - timedelta(hours=hours))
                        .order_by(IotReading.ts)).all()
    return [{"ts": iso(r.ts), "soil_moisture": r.soil_moisture, "air_temp": r.air_temp, "humidity": r.humidity,
             "nitrogen": r.nitrogen, "phosphorus": r.phosphorus, "potassium": r.potassium} for r in rows]


@router.get("/api/admin/otps", dependencies=[Depends(admin_only)])
def demo_otps(session: Session = Depends(get_session)):
    if not OTP_DEMO:
        return []
    return [{"phone": o.phone, "code": o.shown_code, "expires": iso(o.expires)}
            for o in session.exec(select(OtpCode)).all() if aware(o.expires) > utcnow()]


# ---------------------------------------------------------------- demo data for presentations
DEMO_NAMES = ["Ramesh Kumar", "Ravi Kumar", "Suresh Yadav", "Lakshmi Devi", "Venkata Rao", "Srinivas Reddy", "Padma",
              "Nagaraju", "Anjaneyulu", "Sailaja", "Krishna Murthy", "Bhavani", "Prasad Rao", "Durga", "Satyanarayana",
              "Rani", "Koteswara Rao", "Sujatha", "Mallikarjuna", "Vijaya"]


@router.post("/api/admin/demo-data", dependencies=[Depends(admin_only)])
def load_demo(session: Session = Depends(get_session)):
    if session.exec(select(Employee).where(Employee.is_demo == True)).first():  # noqa: E712
        return {"ok": True, "note": "Demo data is already loaded"}
    rnd = random.Random(7)
    emp = Employee(name="Demo Employee (VE-429)", phone="9000000001", is_demo=True)
    session.add(emp)
    session.commit()
    session.refresh(emp)
    emp2 = Employee(name="Demo Employee 2 (VE-430)", phone="9000000002", is_demo=True)
    session.add(emp2)
    session.commit()
    session.refresh(emp2)
    for gid, village, e in [(1, "Nandi (demo)", emp), (2, "Tadikonda (demo)", emp2)]:
        g = session.get(FarmerGroup, gid)
        g.employee_id, g.village = e.id, village
        session.add(g)
    farmers = []
    for i, name in enumerate(DEMO_NAMES):
        gid = 1 if i < 10 else 2
        pkg = "10k" if i % 4 == 0 else "5k"
        base = (16.3067, 80.4365) if gid == 1 else (16.4120, 80.3740)   # around Guntur
        f = Farmer(name=name, phone=f"90000001{i:02d}", group_id=gid, village=session.get(FarmerGroup, gid).village,
                   aadhaar_last4=f"{4500 + i}", bank_account=f"3012{rnd.randint(10**9, 10**10 - 1)}", ifsc="SBIN0001234",
                   survey_no=f"{100 + i}/{rnd.randint(1, 9)}", package=pkg,
                   latitude=base[0] + rnd.uniform(-0.02, 0.02), longitude=base[1] + rnd.uniform(-0.02, 0.02),
                   created_by=emp.id, is_demo=True,
                   **{k: PACKAGES[pkg][k] for k in ("project_cost", "subsidy", "loan_approved", "farmer_share")})
        f.subsidy_received = f.subsidy if i % 3 else round(f.subsidy * 0.6)
        session.add(f)
        farmers.append(f)
    session.commit()
    now = utcnow()
    for gid in (1, 2):
        members = [f for f in farmers if f.group_id == gid]
        bulk = uuid.uuid4().hex[:10]
        for f, share in zip(members, split_amount(18000, len(members))):
            session.add(InputCredit(farmer_id=f.id, item="Tomato seeds (hybrid)", quantity="10 g", amount=share,
                                    given_on=now - timedelta(days=40), given_by=emp.id, bulk_id=bulk))
        for f in members[:4]:
            session.add(InputCredit(farmer_id=f.id, item="Neem oil bio-pesticide", quantity="1 L", amount=450,
                                    given_on=now - timedelta(days=12), given_by=emp.id))
    for i, f in enumerate(farmers[:6]):
        d = IotDevice(id=f"ZM-DEMO-{i + 1:02d}", key=secrets.token_urlsafe(16), farmer_id=f.id, last_seen=now)
        session.add(d)
        for h in range(24, -1, -1):
            dry = i == 2 and h < 3
            session.add(IotReading(device_id=d.id, farmer_id=f.id, ts=now - timedelta(hours=h),
                                   soil_moisture=round(22 + rnd.uniform(-3, 3), 1) if dry else round(48 + 10 * math.sin(h / 4) + rnd.uniform(-3, 3), 1),
                                   air_temp=round(29 + 4 * math.sin(h / 6) + rnd.uniform(-1, 1), 1), humidity=round(65 + rnd.uniform(-8, 8)),
                                   nitrogen=round(rnd.uniform(45, 90)), phosphorus=round(rnd.uniform(18, 40)), potassium=round(rnd.uniform(110, 220))))
        if i == 2:
            session.add(Notice(farmer_id=f.id, kind="moisture", text_te=MOISTURE_ALERT_TE, text_en=MOISTURE_ALERT_EN))
    session.commit()
    # one finished harvest payout (farmer 9000000102), recovered the way pay() does, then a new bill after it
    f3 = farmers[2]
    bills = session.exec(select(InputCredit).where(InputCredit.farmer_id == f3.id, InputCredit.settled_in == None)).all()  # noqa: E711
    gross = 52000.0
    input_ded = round(sum(c.amount for c in bills), 2)
    emi = round(min(f3.loan_approved / EMI_HARVESTS, gross - input_ded), 2)
    po = Payout(farmer_id=f3.id, buyer="BigBasket", crop="Tomato", quantity_kg=2000, rate_per_kg=26, gross=gross,
                input_deduction=input_ded, emi_deduction=emi, net=round(gross - input_ded - emi, 2), status="paid",
                reference="DEMO-PAYOUT-01", created_at=now - timedelta(days=6), paid_at=now - timedelta(days=6))
    session.add(po)
    session.commit()
    session.refresh(po)
    for c in bills:
        c.settled_in = po.id
        session.add(c)
    f3.loan_repaid = emi
    session.add(f3)
    session.add(InputCredit(farmer_id=f3.id, item="Cucumber seeds", quantity="50 g", amount=1200,
                            given_on=now - timedelta(days=3), given_by=emp.id))
    session.add(Notice(farmer_id=f3.id, kind="payment", created_at=now - timedelta(days=6),
                       text_te=f"₹{po.net:,.0f} మీ బ్యాంక్ ఖాతాకు పంపబడింది.", text_en=f"₹{po.net:,.0f} sent to your bank account."))
    session.commit()
    add_demo_attendance(session, [emp, emp2], farmers)
    return {"ok": True, "employee_phone": emp.phone, "employee2_phone": emp2.phone, "farmer_phones": [f.phone for f in farmers[:3]], "farmers": len(farmers)}


@router.delete("/api/admin/demo-data", dependencies=[Depends(admin_only)])
def delete_demo(session: Session = Depends(get_session)):
    farmers = session.exec(select(Farmer).where(Farmer.is_demo == True)).all()  # noqa: E712
    ids = [f.id for f in farmers]
    for model, col in [(InputCredit, InputCredit.farmer_id), (Payout, Payout.farmer_id), (Notice, Notice.farmer_id),
                       (IotReading, IotReading.farmer_id), (IotDevice, IotDevice.farmer_id)]:
        for row in session.exec(select(model).where(col.in_(ids))).all() if ids else []:
            session.delete(row)
    for s in session.exec(select(LoginSession)).all():
        if (s.role == "farmer" and s.user_id in ids):
            session.delete(s)
    for f in farmers:
        session.delete(f)
    emps = session.exec(select(Employee).where(Employee.is_demo == True)).all()  # noqa: E712
    for v in session.exec(select(FieldVisit).where(FieldVisit.employee_id.in_([e.id for e in emps]))).all() if emps else []:
        try:
            (core().UPLOAD_DIR / v.image_path).unlink(missing_ok=True)
        except OSError:
            pass
        session.delete(v)
    for g in session.exec(select(FarmerGroup)).all():
        if g.employee_id in [e.id for e in emps]:
            g.employee_id, g.village = None, None
            session.add(g)
    for s in session.exec(select(LoginSession)).all():
        if s.role == "employee" and s.user_id in [e.id for e in emps]:
            session.delete(s)
    for e in emps:
        session.delete(e)
    session.commit()
    return {"ok": True, "removed_farmers": len(ids)}


# ---------------------------------------------------------------- daily field attendance
def ist_day(dt: datetime) -> str:
    return aware(dt).astimezone(IST).strftime("%Y-%m-%d")


def nearest_farm(session: Session, lat: Optional[float], lon: Optional[float]):
    if lat is None or lon is None:
        return None, None
    best, best_d = None, None
    for f in session.exec(select(Farmer).where(Farmer.latitude != None)).all():  # noqa: E711
        d = metres_between(lat, lon, f.latitude, f.longitude)
        if d is not None and (best_d is None or d < best_d):
            best, best_d = f, d
    return best, (round(best_d) if best_d is not None else None)


def visit_flags(v: FieldVisit) -> list[str]:
    flags = []
    if v.latitude is None:
        flags.append("No GPS")
    elif v.distance_m is None:
        flags.append("No farm GPS to compare")
    elif v.distance_m > FIELD_RADIUS_M:
        flags.append(f"{v.distance_m/1000:.1f} km from nearest farm")
    if v.accuracy_m is not None and v.accuracy_m > GPS_POOR_M:
        flags.append(f"Weak GPS (±{v.accuracy_m:.0f} m)")
    if aware(v.received_at) - aware(v.captured_at) > timedelta(hours=6):
        flags.append("Uploaded late (was offline)")
    return flags


def visit_verified(v: FieldVisit) -> bool:
    return v.latitude is not None and v.distance_m is not None and v.distance_m <= FIELD_RADIUS_M


def visit_dict(session: Session, v: FieldVisit) -> dict:
    f = session.get(Farmer, v.nearest_farmer_id) if v.nearest_farmer_id else None
    return {"id": v.id, "employee_id": v.employee_id, "day": v.day, "captured_at": iso(v.captured_at), "received_at": iso(v.received_at),
            "time_ist": aware(v.captured_at).astimezone(IST).strftime("%I:%M %p").lstrip("0"),
            "latitude": v.latitude, "longitude": v.longitude, "accuracy_m": v.accuracy_m, "image_url": f"/uploads/{v.image_path}",
            "nearest_farmer": f and f.name, "nearest_farmer_id": f and f.id, "nearest_village": f and f.village,
            "farm_lat": f and f.latitude, "farm_lon": f and f.longitude, "distance_m": v.distance_m, "note": v.note,
            "verified": visit_verified(v), "flags": visit_flags(v)}


def summarise_days(visits: list[FieldVisit]) -> list[dict]:
    """Group one employee's photos by IST day: first/last time, hours between, verified or not."""
    days: dict[str, list[FieldVisit]] = {}
    for v in visits:
        days.setdefault(v.day, []).append(v)
    out = []
    for day, vs in sorted(days.items()):
        vs.sort(key=lambda x: aware(x.captured_at))
        first, last = aware(vs[0].captured_at), aware(vs[-1].captured_at)
        verified = any(visit_verified(v) for v in vs)
        out.append({"day": day, "photos": len(vs), "verified": verified,
                    "first": first.astimezone(IST).strftime("%I:%M %p").lstrip("0"),
                    "last": last.astimezone(IST).strftime("%I:%M %p").lstrip("0"),
                    "hours": round((last - first).total_seconds() / 3600, 1) if len(vs) > 1 else 0.0,
                    "flags": sorted({fl for v in vs for fl in visit_flags(v)})})
    return out


def month_range(month: Optional[str]) -> tuple[str, str, str]:
    m = month or datetime.now(IST).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        raise HTTPException(400, "Month must look like 2026-10")
    y, mo = int(m[:4]), int(m[5:])
    nxt = f"{y + (mo == 12)}-{(mo % 12) + 1:02d}"
    return m, f"{m}-01", f"{nxt}-01"


@router.post("/api/emp/field-visit")
async def field_visit(image: UploadFile = File(...), client_id: str = Form(...), captured_at: Optional[str] = Form(None),
                      latitude: Optional[float] = Form(None), longitude: Optional[float] = Form(None),
                      accuracy: Optional[float] = Form(None), note: Optional[str] = Form(None),
                      emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    existing = session.exec(select(FieldVisit).where(FieldVisit.client_id == client_id)).first()
    if existing:
        return visit_dict(session, existing)
    data = await image.read()
    if not data:
        raise HTTPException(400, "Photo is empty")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Photo is larger than 10 MB")
    now = utcnow()
    taken = now
    if captured_at:
        try:
            taken = aware(datetime.fromisoformat(captured_at.replace("Z", "+00:00")))
        except ValueError:
            taken = now
    if taken > now + timedelta(minutes=10):     # phone clock ahead: trust the server
        taken = now
    if now - taken > timedelta(hours=MAX_PHOTO_AGE_HOURS):
        raise HTTPException(400, f"This photo is older than {MAX_PHOTO_AGE_HOURS:.0f} hours and can't count for attendance.")
    day = ist_day(taken)
    rel = f"attendance/{day}/{client_id}.jpg"
    path = core().UPLOAD_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    f, dist = nearest_farm(session, latitude, longitude)
    v = FieldVisit(client_id=client_id, employee_id=emp.id, captured_at=taken, day=day, latitude=latitude, longitude=longitude,
                   accuracy_m=accuracy, image_path=rel, nearest_farmer_id=f and f.id, distance_m=dist,
                   note=(note or "").strip()[:200] or None)
    session.add(v)
    session.commit()
    session.refresh(v)
    out = visit_dict(session, v)
    await core().hub.to_dashboards({"type": "field_visit", "visit": out})
    return out


@router.get("/api/emp/field-visits")
def my_field_visits(month: Optional[str] = None, emp: Employee = Depends(require("employee")), session: Session = Depends(get_session)):
    m, start, end = month_range(month)
    vs = session.exec(select(FieldVisit).where(FieldVisit.employee_id == emp.id, FieldVisit.day >= start, FieldVisit.day < end)
                      .order_by(FieldVisit.captured_at.desc())).all()
    days = summarise_days(list(vs))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return {"month": m, "today": today, "field_days": sum(1 for d in days if d["verified"]),
            "unverified_days": sum(1 for d in days if not d["verified"]), "days": days,
            "today_visits": [visit_dict(session, v) for v in vs if v.day == today], "radius_m": FIELD_RADIUS_M}


@router.get("/api/admin/attendance", dependencies=[Depends(admin_only)])
def attendance(month: Optional[str] = None, session: Session = Depends(get_session)):
    m, start, end = month_range(month)
    out = []
    for e in session.exec(select(Employee).order_by(Employee.name)).all():
        vs = session.exec(select(FieldVisit).where(FieldVisit.employee_id == e.id, FieldVisit.day >= start, FieldVisit.day < end)).all()
        days = summarise_days(list(vs))
        out.append({"employee_id": e.id, "name": e.name, "phone": e.phone, "active": e.active, "is_demo": e.is_demo,
                    "field_days": sum(1 for d in days if d["verified"]), "unverified_days": sum(1 for d in days if not d["verified"]),
                    "photos": len(vs), "hours": round(sum(d["hours"] for d in days), 1), "days": days})
    return {"month": m, "radius_m": FIELD_RADIUS_M, "employees": out}


@router.get("/api/admin/attendance/{employee_id}/{day}", dependencies=[Depends(admin_only)])
def attendance_day(employee_id: int, day: str, session: Session = Depends(get_session)):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(400, "Day must look like 2026-10-12")
    vs = session.exec(select(FieldVisit).where(FieldVisit.employee_id == employee_id, FieldVisit.day == day)
                      .order_by(FieldVisit.captured_at)).all()
    return [visit_dict(session, v) for v in vs]


@router.get("/api/admin/attendance.csv", dependencies=[Depends(admin_only)])
def attendance_csv(month: Optional[str] = None, session: Session = Depends(get_session)):
    data = attendance(month, session)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["employee", "phone", "date", "photos", "first_photo_ist", "last_photo_ist", "hours", "counted_as_field_day", "flags"])
    for e in data["employees"]:
        for d in e["days"]:
            w.writerow([e["name"], e["phone"], d["day"], d["photos"], d["first"], d["last"], d["hours"],
                        "yes" if d["verified"] else "no", "; ".join(d["flags"])])
    w.writerow([])
    w.writerow(["employee", "phone", "field_days", "unverified_days", "photos", "hours"])
    for e in data["employees"]:
        w.writerow([e["name"], e["phone"], e["field_days"], e["unverified_days"], e["photos"], e["hours"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=zemli-attendance-{data['month']}.csv"})


def demo_photo(path: Path, seed: int) -> None:
    """A plain generated picture for demo attendance rows (clearly not a real field photo)."""
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    img = Image.new("RGB", (640, 480), (24 + rnd.randint(0, 20), 80 + rnd.randint(0, 40), 40 + rnd.randint(0, 20)))
    d = ImageDraw.Draw(img)
    for _ in range(14):
        x, y = rnd.randint(0, 640), rnd.randint(0, 480)
        d.ellipse([x - 60, y - 22, x + 60, y + 22], fill=(60 + rnd.randint(0, 60), 140 + rnd.randint(0, 60), 60))
    d.rectangle([0, 430, 640, 480], fill=(10, 30, 20))
    d.text((14, 446), "DEMO FIELD PHOTO", fill=(200, 240, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=80)


def add_demo_attendance(session: Session, emps: list[Employee], farmers: list[Farmer]) -> None:
    rnd = random.Random(11)
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    plans = [(emps[0], [f for f in farmers if f.group_id == 1], 0.85), (emps[1], [f for f in farmers if f.group_id == 2], 0.55)]
    for emp, farms, show_rate in plans:
        for back in range(0, 24):
            day = today - timedelta(days=back)
            if day.weekday() == 6 or rnd.random() > show_rate:   # Sundays off, some missed days
                continue
            far = rnd.random() < 0.15                              # a few photos taken away from any farm
            n = rnd.choice([1, 2, 2, 3])
            start_h = rnd.uniform(8.0, 10.0)
            for k in range(n):
                taken = (day + timedelta(hours=start_h + k * rnd.uniform(2.0, 3.5))).astimezone(timezone.utc)
                if taken > utcnow():
                    continue
                farm = rnd.choice(farms)
                if far:
                    lat, lon = farm.latitude + rnd.uniform(0.05, 0.08), farm.longitude + rnd.uniform(0.05, 0.08)
                else:
                    lat, lon = farm.latitude + rnd.uniform(-0.0015, 0.0015), farm.longitude + rnd.uniform(-0.0015, 0.0015)
                cid = f"demo-{emp.id}-{back}-{k}-{uuid.uuid4().hex[:6]}"
                rel = f"attendance/{ist_day(taken)}/{cid}.jpg"
                demo_photo(core().UPLOAD_DIR / rel, hash(cid) & 0xFFFF)
                f, dist = nearest_farm(session, lat, lon)
                session.add(FieldVisit(client_id=cid, employee_id=emp.id, captured_at=taken, received_at=taken, day=ist_day(taken),
                                       latitude=lat, longitude=lon, accuracy_m=round(rnd.uniform(5, 35)), image_path=rel,
                                       nearest_farmer_id=f and f.id, distance_m=dist, is_demo=True))
    session.commit()
