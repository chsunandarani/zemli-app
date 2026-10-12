"""Zemli crop disease backend (prototype).

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import Scan, get_session, init_db, utcnow
from .inference import ModelRegistry

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.getenv("MODELS_DIR", BASE_DIR / "models"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
API_KEY = os.getenv("ZEMLI_API_KEY", "")          # empty = no key check (local testing only)
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.70"))
ALLOW_EMPLOYEE_UPLOAD = os.getenv("ALLOW_EMPLOYEE_UPLOAD", "1") == "1"   # set 0 to force live camera for staff
PHOTO_TZ = timezone(timedelta(hours=5, minutes=30))   # phones store EXIF time in local time (IST)
GATE_THRESHOLD = float(os.getenv("GATE_THRESHOLD", "0.80"))  # how sure the crop check must be before rejecting
GATE_MIN_CROP = float(os.getenv("GATE_MIN_CROP", "0.10"))    # never reject if the chosen crop still gets at least this much
GATE_AUTO_OTHER = float(os.getenv("GATE_AUTO_OTHER", "0.60"))  # auto mode: "not related" when 'other' is at least this sure
# crops the crop check is used for, e.g. "tomato" or "tomato,cucumber"; empty = every crop the gate model knows
GATE_CROPS = {c.strip() for c in os.getenv("GATE_CROPS", "").split(",") if c.strip()}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

DISEASE_INFO = json.loads((Path(__file__).parent / "disease_info.json").read_text(encoding="utf-8"))
CROPS = DISEASE_INFO["crops"]

app = FastAPI(title="Zemli Crop Disease API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
registry = ModelRegistry(MODELS_DIR)


# ---------- realtime ----------
class Hub:
    """Keeps open WebSockets. Dashboards get every event; an employee gets reviews of their own scans."""

    def __init__(self):
        self.dashboards: set[WebSocket] = set()
        self.employees: dict[str, set[WebSocket]] = {}

    async def send(self, sockets, message: dict):
        for ws in list(sockets):
            try:
                await ws.send_json(message)
            except Exception:
                sockets.discard(ws)

    async def to_dashboards(self, message: dict):
        await self.send(self.dashboards, message)

    async def to_employee(self, employee_id: str, message: dict):
        await self.send(self.employees.get(employee_id, set()), message)


hub = Hub()


# ---------- helpers ----------
def admin_only(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or wrong API key")


def check_key(x_api_key: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Admin API key, or a logged-in Zemli employee (OTP login in the app)."""
    if not API_KEY or x_api_key == API_KEY:
        return
    from .zemli_platform import user_from_auth
    if user_from_auth(authorization, {"employee", "farmer"}):
        return
    raise HTTPException(status_code=401, detail="Missing or wrong API key")


def advice_for(crop: str, label: Optional[str]) -> dict:
    info = CROPS.get(crop, {}).get("classes", {}).get(label or "")
    group = info["group"] if info else "uncertain"
    adv = DISEASE_INFO["advice"][group]
    return {
        "label": label or "uncertain",
        "name_en": info["name_en"] if info else "Not sure",
        "name_te": info["name_te"] if info else "సరిగా గుర్తించలేదు",
        "advice_en": adv["en"],
        "advice_te": adv["te"],
    }


def exif_info(image_bytes: bytes):
    """GPS and 'date taken' stored inside a phone photo, if any. Returns (lat, lon, taken_at)."""
    try:
        from PIL import Image
        exif = Image.open(io.BytesIO(image_bytes)).getexif()
    except Exception:
        return None, None, None
    lat = lon = taken = None
    try:
        gps = exif.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            lat = dms(gps[2]) * (-1 if gps.get(1) == "S" else 1)
            lon = dms(gps[4]) * (-1 if gps.get(3) == "W" else 1)
    except Exception:
        lat = lon = None
    try:
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if raw:
            taken = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S").replace(tzinfo=PHOTO_TZ)
    except Exception:
        taken = None
    return lat, lon, taken


def rejection_text(crop: str, gate_label: Optional[str]) -> dict:
    if gate_label == "other":   # not tomato, cucumber or coriander at all
        m = DISEASE_INFO["messages"]["not_related"]
        return {"label": "not_this_crop", "name_en": m["name_en"], "name_te": m["name_te"],
                "advice_en": m["en"], "advice_te": m["te"]}
    c = CROPS[crop]
    other = CROPS.get(gate_label or "")
    key = "wrong_crop" if other else "not_crop"
    msg = DISEASE_INFO["messages"][key]
    fill = {"crop_en": c["name_en"].split(" (")[0].lower(), "crop_te": c["name_te"],
            "other_en": other["name_en"].split(" (")[0].lower() if other else "",
            "other_te": other["name_te"] if other else ""}
    return {
        "label": "not_this_crop",
        "name_en": f"Not a {fill['crop_en']} leaf",
        "name_te": f"ఇది {c['name_te']} ఆకు కాదు",
        "advice_en": msg["en"].format(**fill),
        "advice_te": msg["te"].format(**fill),
    }


def check_crop(crop: str, image_bytes: bytes) -> Optional[dict]:
    """Runs the crop-check ("gate") model. Returns None when there is no gate model
    or it wasn't trained on this crop (then the photo goes straight to the disease model)."""
    if not registry.has_model("gate") or (GATE_CROPS and crop not in GATE_CROPS):
        return None
    preds = registry.predict("gate", image_bytes, top_k=5)
    if crop not in {p["label"] for p in preds}:
        return None
    top = preds[0]
    crop_prob = next(p["confidence"] for p in preds if p["label"] == crop)
    # Reject only when the model is clearly sure it's something else AND gives the chosen crop almost nothing.
    # Wrongly blocking a real leaf is worse than letting an odd photo through (the agronomist still checks it).
    passed = not (top["label"] != crop and top["confidence"] >= GATE_THRESHOLD and crop_prob < GATE_MIN_CROP)
    return {"label": top["label"], "confidence": top["confidence"], "passed": passed}


def detect_crop(image_bytes: bytes) -> tuple[str, dict]:
    """Auto mode: the crop-check model decides which crop the photo shows.
    Returns (crop, gate) where gate["passed"] is False for a leaf that isn't one of our crops."""
    preds = registry.predict("gate", image_bytes, top_k=5)
    probs = {p["label"]: p["confidence"] for p in preds}
    best_crop = max((c for c in CROPS if c in probs), key=lambda c: probs[c])
    top = preds[0]
    not_related = top["label"] == "other" and top["confidence"] >= GATE_AUTO_OTHER
    return best_crop, {"label": top["label"], "confidence": top["confidence"], "passed": not not_related}


def scan_to_dict(s: Scan) -> dict:
    shown = s.final_label or (
        s.server_label if (s.server_confidence or 0) >= CONFIDENCE_THRESHOLD else None)
    return {
        "id": s.id,
        "client_id": s.client_id,
        "crop": s.crop,
        "employee_id": s.employee_id,
        "farmer_id": s.farmer_id,
        "latitude": s.latitude,
        "longitude": s.longitude,
        "captured_at": s.captured_at.isoformat() if s.captured_at else None,
        "received_at": s.received_at.isoformat() if s.received_at else None,
        "image_url": f"/uploads/{s.image_path}",
        "device_label": s.device_label,
        "device_confidence": s.device_confidence,
        "server_label": s.server_label,
        "server_confidence": s.server_confidence,
        "top_predictions": json.loads(s.top_predictions) if s.top_predictions else [],
        "status": s.status,
        "final_label": s.final_label,
        "review_note": s.review_note,
        "reviewer": s.reviewer,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "gate_label": s.gate_label,
        "source": s.source or "camera",
        "gate_confidence": s.gate_confidence,
        "result": rejection_text(s.crop, s.gate_label)
                  if (s.status == "rejected" or s.final_label == "not_this_crop") else advice_for(s.crop, shown),
    }


# ---------- lifecycle ----------
@app.on_event("startup")
def startup():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    from .zemli_platform import ensure_groups
    ensure_groups()
    if os.getenv("LOAD_DEMO") == "1":   # free hosting resets its data on restart: reload the demo each time
        from sqlmodel import Session as _S
        from .db import engine as _engine
        from .zemli_platform import load_demo
        with _S(_engine) as s:
            print("[zemli] demo data:", load_demo(s))
    loaded = registry.load_all()
    print(f"[zemli] models loaded for: {list(loaded) or 'none (all scans go to manual review)'}")


app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR), check_dir=False), name="uploads")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/sw.js")
def service_worker():
    # served from the site root so the installed app can work for every page
    return FileResponse(Path(__file__).parent / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "dashboard.html")


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    """Lets the Android app (made with PWABuilder) open full screen without a browser bar.
    Paste the assetlinks.json content PWABuilder gives you into the ASSETLINKS_JSON setting."""
    raw = os.getenv("ASSETLINKS_JSON", "")
    if not raw:
        raise HTTPException(404, "Not configured")
    return JSONResponse(json.loads(raw))


@app.get("/app")
def zemli_app():
    return FileResponse(Path(__file__).parent / "static" / "app.html")


@app.get("/admin")
def admin_panel():
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


@app.get("/field")
def field_app():
    return FileResponse(Path(__file__).parent / "static" / "field.html")


@app.get("/api/health")
def health():
    return {"ok": True, "crop_check": registry.has_model("gate"),
            "models": {c: registry.has_model(c) for c in CROPS}}


@app.get("/api/crops")
def crops():
    return {
        "threshold": CONFIDENCE_THRESHOLD,
        "crop_check": registry.has_model("gate"),
        "auto_detect": registry.has_model("gate"),
        "crops": [
            {"id": cid, "name_en": c["name_en"], "name_te": c["name_te"],
             "has_model": registry.has_model(cid), "classes": list(c["classes"])}
            for cid, c in CROPS.items()
        ],
    }


@app.post("/api/models/reload", dependencies=[Depends(admin_only)])
def reload_models():
    return {"loaded": registry.load_all()}


@app.post("/api/scans", dependencies=[Depends(check_key)])
async def create_scan(
    image: UploadFile = File(...),
    crop: str = Form(...),
    employee_id: str = Form(...),
    client_id: str = Form(default_factory=lambda: str(uuid.uuid4())),
    farmer_id: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    captured_at: Optional[str] = Form(None),
    device_label: Optional[str] = Form(None),
    device_confidence: Optional[float] = Form(None),
    source: str = Form("camera"),
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
):
    from .zemli_platform import MAX_PHOTO_AGE_HOURS, user_from_auth
    logged_in = user_from_auth(authorization, {"employee", "farmer"})
    if logged_in and logged_in[0] == "employee":
        employee_id = f"E{logged_in[1].id}"   # taken from the login, not from the form, so it can't be faked
    elif logged_in:   # a farmer scanning their own crop: always saved under their own ID
        farmer_id = str(logged_in[1].id)
        employee_id = f"farmer:{logged_in[1].id}"   # also the farmer's live channel, so the agronomist's answer reaches them
    source = "upload" if source == "upload" else "camera"
    if source == "upload" and logged_in and logged_in[0] == "employee" and not ALLOW_EMPLOYEE_UPLOAD:
        raise HTTPException(400, "Uploads are switched off for staff. Use the live camera.")
    if crop == "auto" and not registry.has_model("gate"):
        raise HTTPException(400, "Auto-detect needs the crop-check model (models/gate). Pick the crop instead.")
    if crop != "auto" and crop not in CROPS:
        raise HTTPException(400, f"Unknown crop '{crop}'. Use auto or one of: {', '.join(CROPS)}")

    existing = session.exec(select(Scan).where(Scan.client_id == client_id)).first()
    if existing:  # phone retried after a network drop; don't create a duplicate
        return scan_to_dict(existing)

    data = await image.read()
    if not data:
        raise HTTPException(400, "Image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image is larger than 10 MB")

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rel_path = f"{day}/{client_id}.jpg"
    (UPLOAD_DIR / day).mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / rel_path).write_bytes(data)

    try:
        if crop == "auto":
            crop, gate = detect_crop(data)   # crop becomes the detected crop
        else:
            gate = check_crop(crop, data)
        preds = registry.predict(crop, data) if (gate is None or gate["passed"]) else None
    except Exception as exc:  # unreadable image etc.
        raise HTTPException(400, f"Could not read image: {exc}")

    top = preds[0] if preds else None
    confident = bool(top and top["confidence"] >= CONFIDENCE_THRESHOLD)
    if gate is not None and not gate["passed"]:
        status = "rejected"   # not this crop / not a leaf: don't run the disease model
    elif confident and top["label"] == "healthy":
        status = "auto_healthy"
    else:
        status = "needs_review"

    parsed_time = None
    if captured_at:
        try:
            parsed_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_time = None
    if source == "upload":
        # an uploaded file: use the GPS and 'date taken' saved inside the photo, not the phone's position now
        ex_lat, ex_lon, ex_time = exif_info(data)
        latitude, longitude, parsed_time = ex_lat, ex_lon, ex_time
    if (logged_in and logged_in[0] == "employee" and parsed_time
            and (datetime.now(timezone.utc) - parsed_time).total_seconds() > MAX_PHOTO_AGE_HOURS * 3600):
        raise HTTPException(400, f"This photo was taken more than {MAX_PHOTO_AGE_HOURS:.0f} hours ago. Take a new photo.")

    scan = Scan(
        client_id=client_id, crop=crop, employee_id=employee_id, farmer_id=farmer_id,
        latitude=latitude, longitude=longitude, captured_at=parsed_time, image_path=rel_path,
        device_label=device_label, device_confidence=device_confidence,
        server_label=top["label"] if top else None,
        server_confidence=top["confidence"] if top else None,
        top_predictions=json.dumps(preds) if preds else None,
        gate_label=gate["label"] if gate else None,
        gate_confidence=gate["confidence"] if gate else None,
        source=source,
        status=status,
    )
    session.add(scan)
    session.commit()
    session.refresh(scan)

    payload = scan_to_dict(scan)
    await hub.to_dashboards({"type": "scan_created", "scan": payload})
    return payload


@app.get("/api/scans", dependencies=[Depends(check_key)])
def list_scans(
    status: Optional[str] = None,
    crop: Optional[str] = None,
    employee_id: Optional[str] = None,
    farmer_id: Optional[str] = None,
    limit: int = Query(100, le=500),
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
):
    q = select(Scan)
    from .zemli_platform import user_from_auth
    who = user_from_auth(authorization, {"farmer"})
    if who:
        q = q.where(Scan.farmer_id == str(who[1].id))
    if status:
        q = q.where(Scan.status == status)
    if crop:
        q = q.where(Scan.crop == crop)
    if employee_id:
        q = q.where(Scan.employee_id == employee_id)
    if farmer_id:
        q = q.where(Scan.farmer_id == farmer_id)
    rows = session.exec(q.order_by(Scan.received_at.desc()).limit(limit)).all()
    return [scan_to_dict(s) for s in rows]


class Review(BaseModel):
    final_label: str
    reviewer: str
    note: Optional[str] = None


@app.post("/api/scans/{scan_id}/review", dependencies=[Depends(admin_only)])
async def review_scan(scan_id: int, body: Review, session: Session = Depends(get_session)):
    scan = session.get(Scan, scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    valid = list(CROPS[scan.crop]["classes"]) + ["unclear_photo", "not_this_crop"]
    if body.final_label not in valid:
        raise HTTPException(400, f"Label must be one of: {', '.join(valid)}")

    scan.final_label = body.final_label
    scan.reviewer = body.reviewer
    scan.review_note = body.note
    scan.reviewed_at = utcnow()
    scan.status = "verified"
    session.add(scan)
    session.commit()
    session.refresh(scan)

    payload = scan_to_dict(scan)
    await hub.to_dashboards({"type": "scan_reviewed", "scan": payload})
    await hub.to_employee(scan.employee_id, {"type": "scan_reviewed", "scan": payload})
    # Production: send an FCM push / SMS to the farmer here.
    return payload


@app.get("/api/export/verified.csv", dependencies=[Depends(admin_only)])
def export_verified(session: Session = Depends(get_session)):
    """Agronomist-verified labels -> CSV, used by training/import_verified.py to grow the dataset."""
    rows = session.exec(select(Scan).where(Scan.status == "verified")).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["image_path", "crop", "final_label", "server_label", "server_confidence",
                "latitude", "longitude", "captured_at", "employee_id"])
    for s in rows:
        w.writerow([s.image_path, s.crop, s.final_label, s.server_label, s.server_confidence,
                    s.latitude, s.longitude, s.captured_at, s.employee_id])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=verified.csv"})


@app.websocket("/ws")
async def websocket(ws: WebSocket, role: str = "dashboard", employee_id: str = "", key: str = "", token: str = ""):
    if token:   # app users: the channel comes from their login, never from the URL
        from .zemli_platform import user_from_auth
        u = user_from_auth(f"Bearer {token}", {"employee", "farmer"})
        if not u:
            await ws.close(code=4401)
            return
        role, employee_id = "employee", (f"E{u[1].id}" if u[0] == "employee" else f"farmer:{u[1].id}")
    elif API_KEY and key != API_KEY:
        await ws.close(code=4401)
        return
    await ws.accept()
    group = hub.dashboards if role == "dashboard" else hub.employees.setdefault(employee_id, set())
    group.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from the client
    except WebSocketDisconnect:
        group.discard(ws)


from .zemli_platform import router as platform_router  # noqa: E402  (after hub/registry exist)
app.include_router(platform_router)
