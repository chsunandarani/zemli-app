"""Crop and disease identification with Google Gemini (vision).

Turned on by setting GEMINI_API_KEY on the server. The key stays on the server: phones never see it.
If Gemini is slow, over its limit or returns something unusable, analyze() returns None and the
caller falls back to the local ONNX models (or sends the scan to the agronomist).

Settings (environment variables):
  GEMINI_API_KEY   key from https://aistudio.google.com/apikey
  GEMINI_MODEL     default "gemini-flash-latest" (any vision model id, e.g. gemini-2.5-flash)
  AI_ENGINE        "auto" (Gemini when a key is set, else local models), "gemini", or "local"
"""
from __future__ import annotations

import base64
import io
import json
import os
from typing import Optional

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
AI_ENGINE = os.getenv("AI_ENGINE", "auto").strip().lower()
TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT", "45"))
ALL_CROPS = os.getenv("ALL_CROPS", "1") == "1"   # 1 = diagnose any crop; 0 = only the pilot crops
OTHER = "other_crop"

last_error: Optional[str] = None   # shown on /api/health so problems are easy to spot


def enabled() -> bool:
    return bool(GEMINI_API_KEY) and AI_ENGINE in ("auto", "gemini")


def all_crops() -> bool:
    return enabled() and ALL_CROPS


def _pilot(crops: dict) -> dict:
    return {k: v for k, v in crops.items() if k != OTHER}


def _prepare_image(data: bytes) -> bytes:
    """Shrink to 1024 px and re-encode: faster, cheaper, and drops EXIF (no GPS sent to Google)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1024, 1024))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _prompt(chosen_crop: Optional[str], crops: dict) -> str:
    lines = []
    for cid, c in _pilot(crops).items():
        names = ", ".join(f"{k} ({v['name_en']})" for k, v in c["classes"].items())
        lines.append(f"- {cid} = {c['name_en']}: {names}")
    picked = (f"The field worker says this is {crops[chosen_crop]['name_en']}. Check that it really is; "
              f"if it is another crop, say which.") if chosen_crop and chosen_crop != OTHER else "The field worker did not say which crop this is."
    if ALL_CROPS:
        other_rule = f"""   If it is a leaf or plant of ANY OTHER crop or plant (for example chilli, brinjal, okra, paddy, cotton, maize,
   groundnut, banana, mango, a vegetable, fruit tree or weed), use crop "{OTHER}", put the plant's common name in
   crop_name_en and its Telugu name in crop_name_te, and use label "healthy", "other_problem" (any disease, pest or
   deficiency, named in name_en / name_te) or "unclear".
   Use crop "other" ONLY when the photo shows no plant at all (a person, an object, soil, a wall, a screen)."""
    else:
        other_rule = """   If it is any other plant, a fruit only, a person, an object, soil, or not a plant at all, use crop "other"."""
    return f"""You are a plant pathologist helping small farmers in Guntur, Andhra Pradesh, India. Their main crops are
tomato, cucumber (keera dosakaya) and coriander (kothimeera), grown inside net-sheds.

Look at the photo and decide:
1. Which plant is it? If it is tomato, cucumber or coriander, use that crop id.
{other_rule}
2. Which condition best matches what you can actually see. For the three main crops use ONLY these labels:
{chr(10).join(lines)}
   Use "other_problem" if you clearly see a problem that is none of these (for example a nutrient deficiency,
   insect damage, sunscald or a different disease). Use "unclear" if the photo is too blurry, dark or far away to judge.
   Always fill crop_name_en and crop_name_te with the plant's name (or "None" if there is no plant).
{picked}

Rules:
- Base the answer only on visible symptoms. Do not guess. If symptoms are early or ambiguous, lower the confidence.
- confidence is your honest probability (0 to 1) that the label is correct.
- name_te and advice_te must be simple Telugu a farmer understands. advice must be practical, safe steps
  (remove affected leaves, keep leaves dry, sticky traps, neem-based or bio products) and must say to ask the
  Zemli agronomist before spraying any chemical. Never give chemical doses.
- Keep explanation_en to one or two short sentences about the symptoms you see."""


def _schema(crops: dict) -> dict:
    labels = sorted({k for c in crops.values() for k in c["classes"]} | {"other_problem", "unclear"})
    crop_ids = list(_pilot(crops)) + ([OTHER] if ALL_CROPS else []) + ["other"]
    return {
        "type": "OBJECT",
        "properties": {
            "crop": {"type": "STRING", "enum": crop_ids},
            "crop_name_en": {"type": "STRING"},
            "crop_name_te": {"type": "STRING"},
            "is_plant_leaf": {"type": "BOOLEAN"},
            "label": {"type": "STRING", "enum": labels},
            "confidence": {"type": "NUMBER"},
            "name_en": {"type": "STRING"},
            "name_te": {"type": "STRING"},
            "explanation_en": {"type": "STRING"},
            "advice_en": {"type": "STRING"},
            "advice_te": {"type": "STRING"},
            "severity": {"type": "STRING", "enum": ["none", "mild", "moderate", "severe"]},
        },
        "required": ["crop", "crop_name_en", "crop_name_te", "is_plant_leaf", "label", "confidence", "name_en", "name_te",
                     "explanation_en", "advice_en", "advice_te", "severity"],
    }


async def analyze(image_bytes: bytes, chosen_crop: Optional[str], crops: dict) -> Optional[dict]:
    """Returns a cleaned result dict, or None (caller falls back)."""
    global last_error
    if not enabled():
        return None
    try:
        jpeg = _prepare_image(image_bytes)
    except Exception as exc:
        last_error = f"could not read image: {exc}"
        return None
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": _prompt(chosen_crop, crops)},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
        ]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "responseSchema": _schema(crops)},
    }
    url = f"{GEMINI_BASE_URL}/models/{GEMINI_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY})
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            last_error = f"HTTP {r.status_code}: {msg}"
            print(f"[zemli] Gemini error {last_error}")
            return None
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        out = json.loads(text)
    except Exception as exc:   # timeout, network, bad JSON
        last_error = f"{type(exc).__name__}: {exc}"
        print(f"[zemli] Gemini failed: {last_error}")
        return None

    valid_crops = list(_pilot(crops)) + ([OTHER] if ALL_CROPS and OTHER in crops else [])
    crop = out.get("crop") if out.get("crop") in valid_crops + ["other"] else "other"
    if not out.get("is_plant_leaf", True) and crop != OTHER:
        crop = "other"
    label = out.get("label")
    if crop == OTHER:
        allowed = {"healthy", "other_problem", "unclear"}
        if label not in allowed:   # a pilot-crop disease name used for another crop: keep it as "other problem"
            label = "unclear" if label == "unclear" else ("healthy" if label == "healthy" else "other_problem")
    else:
        allowed = set(crops[crop]["classes"]) | {"other_problem", "unclear"} if crop in crops else set()
    if label not in allowed:
        label = "unclear" if crop in crops else None
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    last_error = None
    cn_en = str(out.get("crop_name_en", "")).strip()[:60]
    cn_te = str(out.get("crop_name_te", "")).strip()[:60]
    if crop in _pilot(crops):
        cn_en, cn_te = crops[crop]["name_en"], crops[crop]["name_te"]
    return {
        "crop": crop, "crop_name_en": cn_en, "crop_name_te": cn_te, "label": label, "confidence": round(conf, 4),
        "name_en": str(out.get("name_en", ""))[:120], "name_te": str(out.get("name_te", ""))[:120],
        "explanation_en": str(out.get("explanation_en", ""))[:400],
        "advice_en": str(out.get("advice_en", ""))[:600], "advice_te": str(out.get("advice_te", ""))[:800],
        "severity": out.get("severity") if out.get("severity") in ("none", "mild", "moderate", "severe") else None,
        "model": GEMINI_MODEL,
    }
