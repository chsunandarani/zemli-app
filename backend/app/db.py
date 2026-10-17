import os
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./zemli.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: str = Field(index=True, unique=True)  # generated on the phone; makes offline re-uploads safe
    crop: str = Field(index=True)
    employee_id: str = Field(index=True)
    farmer_id: Optional[str] = Field(default=None, index=True)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    captured_at: Optional[datetime] = None
    received_at: datetime = Field(default_factory=utcnow)
    image_path: str

    device_label: Optional[str] = None
    device_confidence: Optional[float] = None
    server_label: Optional[str] = None
    server_confidence: Optional[float] = None
    top_predictions: Optional[str] = None  # JSON string

    gate_label: Optional[str] = None       # what the crop-check model thinks the photo shows
    gate_confidence: Optional[float] = None
    source: Optional[str] = None            # "camera" (live, in the app) or "upload" (file / gallery)
    ai_engine: Optional[str] = None         # "gemini" or "local" (trained models)
    ai_note: Optional[str] = None           # JSON from Gemini: names, explanation, advice, severity, model

    status: str = Field(default="needs_review", index=True)  # needs_review | auto_healthy | rejected | verified
    final_label: Optional[str] = None
    review_note: Optional[str] = None
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Zemli platform tables (employees, groups, farmers, khata, IoT, payouts, login)
# ---------------------------------------------------------------------------
class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str = Field(index=True, unique=True)   # 10 digits, no +91
    active: bool = True
    is_demo: bool = False


class FarmerGroup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)   # Group 1 .. Group 10
    name: str
    village: Optional[str] = None
    employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")


class Farmer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str = Field(index=True, unique=True)
    group_id: int = Field(foreign_key="farmergroup.id", index=True)
    village: Optional[str] = None
    aadhaar_last4: Optional[str] = None   # only the last 4 digits are ever stored
    bank_account: Optional[str] = None
    ifsc: Optional[str] = None
    survey_no: Optional[str] = None
    package: str = "5k"                   # "5k" or "10k" sq. ft.
    latitude: Optional[float] = None      # farm location, used for the map and the photo audit
    longitude: Optional[float] = None
    project_cost: float = 0
    subsidy: float = 0
    subsidy_received: float = 0
    loan_approved: float = 0
    loan_repaid: float = 0
    farmer_share: float = 0
    created_at: datetime = Field(default_factory=utcnow)
    created_by: Optional[int] = None
    is_demo: bool = False


class InputCredit(SQLModel, table=True):
    """Khata book: seeds / bio-pesticides given on interest-free credit."""
    id: Optional[int] = Field(default=None, primary_key=True)
    farmer_id: int = Field(foreign_key="farmer.id", index=True)
    item: str
    quantity: Optional[str] = None
    amount: float
    given_on: datetime = Field(default_factory=utcnow)
    given_by: Optional[int] = None
    bulk_id: Optional[str] = None         # same value for every row of one "Bulk Give"
    settled_in: Optional[int] = None      # payout id that recovered this bill


class IotDevice(SQLModel, table=True):
    id: str = Field(primary_key=True)     # printed on the box, e.g. ZM-0001
    key: str
    farmer_id: Optional[int] = Field(default=None, foreign_key="farmer.id")
    last_seen: Optional[datetime] = None


class IotReading(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)
    farmer_id: Optional[int] = Field(default=None, index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    soil_moisture: Optional[float] = None   # %
    air_temp: Optional[float] = None        # deg C
    humidity: Optional[float] = None        # %
    nitrogen: Optional[float] = None        # mg/kg
    phosphorus: Optional[float] = None
    potassium: Optional[float] = None


class Notice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    farmer_id: int = Field(index=True)
    kind: str                     # moisture | disease | payment
    text_te: str
    text_en: str
    created_at: datetime = Field(default_factory=utcnow)


class Payout(SQLModel, table=True):
    """Money splitter: buyer payment -> minus seed bill -> minus loan EMI -> profit to farmer."""
    id: Optional[int] = Field(default=None, primary_key=True)
    farmer_id: int = Field(foreign_key="farmer.id", index=True)
    buyer: str
    crop: Optional[str] = None
    quantity_kg: Optional[float] = None
    rate_per_kg: Optional[float] = None
    gross: float
    input_deduction: float
    emi_deduction: float
    net: float
    status: str = "calculated"    # calculated | paid
    reference: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    paid_at: Optional[datetime] = None


class OtpCode(SQLModel, table=True):
    phone: str = Field(primary_key=True)
    code_hash: str
    expires: datetime
    attempts: int = 0
    shown_code: Optional[str] = None   # demo mode only: kept so the admin panel can show it


class LoginSession(SQLModel, table=True):
    token: str = Field(primary_key=True)
    role: str                      # employee | farmer
    user_id: int
    expires: datetime


class FieldVisit(SQLModel, table=True):
    """Daily field photo (attendance): live camera, GPS, time. One or more per day."""
    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: str = Field(index=True, unique=True)   # made on the phone, so offline re-uploads never duplicate
    employee_id: int = Field(foreign_key="employee.id", index=True)
    captured_at: datetime = Field(index=True)         # when the photo was taken
    received_at: datetime = Field(default_factory=utcnow)
    day: str = Field(index=True)                      # IST date "YYYY-MM-DD" of captured_at
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    image_path: str
    nearest_farmer_id: Optional[int] = None
    distance_m: Optional[float] = None
    note: Optional[str] = None
    is_demo: bool = False


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    # create_all doesn't add new columns to an existing table, so add them by hand (SQLite and Postgres)
    from sqlalchemy import inspect, text
    have = {c["name"] for c in inspect(engine).get_columns("scan")}
    with engine.begin() as conn:
        for name, sql_type in [("gate_label", "VARCHAR"), ("gate_confidence", "FLOAT"), ("source", "VARCHAR"), ("ai_engine", "VARCHAR"), ("ai_note", "TEXT")]:
            if name not in have:
                conn.execute(text(f"ALTER TABLE scan ADD COLUMN {name} {sql_type}"))


def get_session():
    with Session(engine) as session:
        yield session
