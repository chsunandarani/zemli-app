# Zemli live-trial app: how to run and demo it

Everything runs from the same backend you already start with uvicorn.

| Who | Open | Login |
|---|---|---|
| Field employees and farmers (one app) | `http://localhost:8000/app` | phone number + OTP |
| Admin (you) | `http://localhost:8000/admin` | admin API key (`ZEMLI_API_KEY`) |
| Agronomist disease review | `http://localhost:8000/` | admin API key |

The phone number decides the screen: an employee's number opens the Employee screen, a farmer's number opens the Farmer screen.

## First run (5 minutes)

1. Start the server as usual, then open `http://localhost:8000/admin`.
2. **Demo & login codes → Load demo data.** This adds two demo employees (9000000001 and 9000000002) with three weeks of demo field photos, 20 farmers in Groups 1 and 2 around Guntur, khata entries and 6 IoT boxes.
3. Open `http://localhost:8000/app`, enter **9000000001** (demo employee), tap Send OTP.
   No SMS provider is connected yet, so the code is shown on screen ("demo mode") and in the admin panel.
4. To see the farmer screen, log out and log in with **9000000102** (a demo farmer).

Use the cloudflared `https://` address on a phone for the camera and GPS.

## What each module does

**Module 1: Village Employee**
- Group tabs 1 to 10, each showing up to 10 farmers.
- Add farmer: name, phone, village, Aadhaar (only the last 4 digits are stored), bank account, IFSC, survey number, 5k / 10k package, farm GPS.
- Photo: after choosing the crop, the user picks **Camera** (live photo, GPS and time recorded at the shutter) or **Upload photo** (gallery or files; the GPS and date saved inside the photo are used when present). Uploaded photos are marked on the review desk and flagged in the daily photo audit. To force the live camera for staff again, start the server with `ALLOW_EMPLOYEE_UPLOAD=0`. Live photos: the scan is saved under the farmer's ID, and the employee ID comes from the login. Photos older than 48 hours are refused. Offline: photos wait on the phone and upload automatically.
- Khata: give inputs to one farmer, or **Bulk Give** to split a cost equally across the group (exact to the paisa).

**Daily field attendance (employees)**
- Employee app → **Field audit** tab → big glowing button **Capture daily field photo**. Live camera only (no gallery), with the employee ID, date/time and GPS shown on the camera screen. Works offline: the photo waits on the phone and uploads later with its original time.
- A day counts as a **field day** when at least one photo was taken within 500 m of a registered farm (`FIELD_RADIUS_M`). Photos without GPS or away from all farms are saved but marked "not verified". Photos older than 48 hours are refused.
- The employee sees today's status and a monthly calendar of counted days.
- Admin → **Field attendance**: field days per employee for any month, unverified days, total photos, hours between first and last photo, a calendar per employee, and each day's photos with time, GPS link, nearest farm and distance on a map. **Download CSV** gives the month day by day (for salary or allowance).
- Farms need their GPS set (employee app → farmer → "Set farm GPS here") for photos near them to count.

**Module 2: Farmer**
- Loan & subsidy: project cost, subsidy received vs approved, loan approved / repaid / remaining, with progress bars.
- Input bills: every item with date, and the pending total.
- My farm: green / red lights for soil moisture, weather and nutrients from the IoT box, plus the history of AI disease scans with the Telugu advice.
- Payment slips: Total income − Seed bill − Loan EMI = Profit sent to bank.
- Scan (స్కాన్): farmers can photograph their own crop with the same live camera and get the AI result and Telugu advice at once. The scan is always saved under their own ID, goes to the agronomist's review desk, and the agronomist's answer pops up on the farmer's phone. Farmers can only see their own scans. In the photo audit these show as "Farmer self-scan" and are not flagged for distance.
- Telugu alert "పొలంలో తడి తగ్గింది, వెంటనే డ్రిప్ ఆన్ చేయండి." appears when the soil is dry (at most once every 6 hours).

**Module 3: Admin panel**
- Farm map (OpenStreetMap; switch to Google Maps later if you get an API key).
- Daily photo audit: all geo-tagged photos per day and employee, flagged when taken over 500 m from the farm or without GPS.
- Farmers: edit subsidy received, subsidy approved and loan approved as approvals come in.
- Employees & groups: add employees, assign each group to one.
- Money splitter & DBT: enter the buyer payment per farmer (quantity × rate), see the deductions, create payouts, "Send to bank".
- IoT devices: register a box for a farmer and get its key.

## Disease detection with Gemini (optional, recommended for the prototype)

Set `GEMINI_API_KEY` on the server (key from https://aistudio.google.com/apikey). Every scan is then sent to
Gemini, which identifies the crop (tomato / cucumber / coriander / not related) and the disease, with a short
explanation and Telugu advice. Coriander works too, without a trained model.

- The key lives only on the server, never in the app or APK.
- Photos are shrunk to 1024 px and their hidden GPS/EXIF data removed before being sent to Google.
- If Gemini fails or hits its free limit, the trained models are used instead; `/api/health` shows the last Gemini error.
- Diseases always go to the agronomist's review desk. For known diseases the app shows the agronomist-reviewed advice
  from `disease_info.json`; for problems outside that list ("other problem") it shows Gemini's advice, marked as AI advice.
- **Any crop:** besides tomato, cucumber and coriander, Gemini also diagnoses any other crop (chilli, brinjal, paddy, cotton…).
  These are saved as "Other crop" with the real crop name in English and Telugu, and the advice is marked as AI advice.
  Only photos with no plant at all are rejected. If someone picks the wrong crop, the result is given for the crop actually
  seen, with a note. Set `ALL_CROPS=0` to limit it to the three pilot crops again.
- `GEMINI_MODEL` (default `gemini-flash-latest`) chooses the model. `AI_ENGINE=local` switches Gemini off.
- On the free tier Google may use the photos to improve its products, and daily limits apply. Use a paid key for the real pilot.

## IoT

- `iot/esp32_zemli_sensor.ino`: ESP32 + 4G (SIM7600 / A7670C) box that reads soil moisture, DHT22 temperature/humidity and an RS485 NPK sensor every 15 minutes and posts to `/api/iot/reading`. Prototype code: calibrate the moisture sensor and check your NPK sensor's Modbus registers.
- `iot/simulate_iot.py`: pretend to be a box for demos. Add `--dry` to trigger the Telugu alert.

## What is still prototype-only (must change before the real trial)

1. **OTP by SMS**: plug an SMS provider into `send_sms_otp()` in `backend/app/zemli_platform.py` (needs a DLT-registered template in India), then start the server with `OTP_DEMO=0`.
2. **Real money transfers**: "Send to bank" only records the payment. A licensed escrow / payout partner and the bank must do the actual transfer; call their API in `pay()` and store their reference.
3. **Personal data**: bank account numbers are stored in the database. Use HTTPS, a strong admin key, restricted server access and backups; encrypt the database or that column before scaling up. Full Aadhaar numbers are never stored.
4. **EMI rule**: the default EMI per harvest is loan ÷ 14 (two harvests a year for 7 years), capped so it never exceeds what's left after the seed bill. Agree the real repayment schedule with the lender and change `EMI_HARVESTS` or use the EMI override column.
5. **IoT thresholds** (dry below 30% moisture, hot above 38 °C, NPK low limits) are starting values for the agronomist to tune.
6. A determined person could still photograph a photo on another screen. The daily audit (GPS distance and time) is how you catch that.
