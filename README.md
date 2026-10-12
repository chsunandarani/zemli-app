# Zemli prototype

**Start here: `APP_GUIDE.md`** explains the full app (employee + farmer app at `/app`, admin panel at `/admin`) and how to demo it.

## Crop disease part

Real-time disease checking for the pilot crops: tomato, keera dosakaya (cucumber) and kothimeera (coriander).

```
backend/    FastAPI server: receives scans, runs the server model, live dashboard, agronomist review
mobile/     Flutter field app: camera, GPS, on-phone model, works offline, uploads when online
training/   Train one model per crop, export to ONNX (server) and TFLite (phone)
```

## How a scan moves through the system

1. Field employee picks the crop and photographs a leaf.
2. The phone model gives a result in about a second, in Telugu and English, even with no signal.
3. The scan (photo, GPS, time, phone's guess) is saved on the phone and uploads when there is network.
4. The server runs its own copy of the model and the scan appears instantly on the agronomist's dashboard.
5. The agronomist confirms or corrects the diagnosis. The answer reaches the employee's phone straight away.
6. Verified photos are exported back into training, so the models keep improving with real field data.

Results below 70% confidence are shown as "not sure" and always go to the agronomist.

**Crop check:** if `backend/models/gate/` exists, every photo is first checked by a small model
(tomato / cucumber / coriander / other). If it is at least 60% sure the photo shows a different crop
or something that isn't one of our leaves, the scan is marked "Not this crop", the disease model is
skipped, and the employee is asked to retake the photo in Telugu and English. The agronomist can still
override it on the dashboard. Train it with `training/train_crop_check_colab.ipynb`.
Settings: `GATE_THRESHOLD` (default 0.60).
Crops without a trained model (coriander at first) still work: photos are collected and labelled by hand.

## 1. Run the backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ZEMLI_API_KEY=choose-a-long-secret              # Windows: set ZEMLI_API_KEY=...
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 for the dashboard. Put trained models in `backend/models/<crop>/model.onnx` and `labels.txt`, then restart or call `POST /api/models/reload`.

Settings (environment variables): `ZEMLI_API_KEY`, `DATABASE_URL` (default SQLite; use a Postgres URL later), `CONFIDENCE_THRESHOLD` (default 0.70), `MODELS_DIR`, `UPLOAD_DIR`.

API summary: `POST /api/scans` (multipart upload), `GET /api/scans`, `POST /api/scans/{id}/review`, `GET /api/export/verified.csv`, `GET /api/crops`, `GET /api/health`, WebSocket `/ws?role=dashboard` or `/ws?role=employee&employee_id=...`. Interactive docs at `/docs`.

## 2. Train the models

```bash
cd training
pip install -r requirements.txt
python train.py --crop tomato --data data/tomato --epochs 15
cp -r out/tomato ../backend/models/tomato
./convert_tflite.sh tomato        # puts tomato.tflite + labels into mobile/assets/models
```

Class folder names must match the keys in `backend/app/disease_info.json`
(for example `data/cucumber/train/downy_mildew/`). `metrics.json` reports validation,
public test and field test results separately. Quote the field test number as the real-world accuracy.

Pull agronomist-verified pilot photos into the dataset:
```bash
python import_verified.py --server http://localhost:8000 --key YOUR_KEY --dest data
```

## 3. Build the field app

```bash
flutter create --org com.zemliagritech --project-name zemli_field zemli_field
# copy mobile/lib, mobile/assets and mobile/pubspec.yaml into the new project, replacing the defaults
cd zemli_field && flutter pub get
```

Android: in `android/app/src/main/AndroidManifest.xml` add, above `<application>`:
```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.CAMERA"/>
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION"/>
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION"/>
```
For testing against a plain `http://` server on your laptop, also add `android:usesCleartextTraffic="true"` to the `<application>` tag (remove it once you use https).
Set `minSdk = 26` in `android/app/build.gradle` (tflite_flutter needs it).

iOS: add `NSCameraUsageDescription` and `NSLocationWhenInUseUsageDescription` to `ios/Runner/Info.plist`.

Run with `flutter run`, or build an APK with `flutter build apk --release`. On first launch, enter the server address (on the same WiFi use your laptop's IP, like `http://192.168.1.20:8000`), the API key and the employee ID.

## Putting it online for the pilot

Any small Linux server works (a 1-2 GB VPS is enough for the prototype). Run uvicorn behind Caddy or Nginx with https, so the phone uses `https://` and the WebSocket uses `wss://` automatically. Keep `uploads/` and the database on a persistent disk and back them up.

## Before field use

- An agronomist must review every disease name and advice line in `disease_info.json`, especially the Telugu.
- Uploaded photos are served from `/uploads/` without the API key. Fine for a closed pilot; add auth before going public.
- The dashboard has one shared API key. Give each agronomist their own login before scaling up.
- Phone push notifications (FCM) and SMS for farmers are not included yet; the backend marks the spot in `review_scan()`.
