# Share Zemli with your team: free online link + Android APK

An APK of this app is a wrapper around the web app, so the server must be online at a fixed address.
This guide uses **Render**. `render.yaml` is set up for the always-on plan: a Starter instance (about $7/month) that never sleeps, plus a 1 GB disk so data is kept. To test for free instead, change `plan: starter` to `plan: free` and delete the four `disk:` lines (then it sleeps after 15 minutes and data resets).

## Step 1: Put the code on GitHub (about 10 minutes)

1. Extract the latest `zemli-prototype.zip` into a **new** folder, for example `D:\zemli-upload`.
   (Don't upload from `D:\zemli-prototype`: it contains `.venv`, thousands of files you must not upload.)
2. Copy your trained models into it: copy the folder `D:\zemli-prototype\zemli\backend\models`
   to `D:\zemli-upload\zemli\backend\models` (it holds `tomato`, `cucumber`, `gate`).
3. On github.com: **+ → New repository**, name `zemli-app`, choose **Private**, click **Create repository**.
4. On the new repository page click **uploading an existing file**. Open `D:\zemli-upload\zemli`, select
   **everything inside it** (`backend`, `iot`, `mobile`, `training`, `render.yaml`, `.gitignore` and the .md files)
   and drag it into the browser. Click **Commit changes**.
   Check that `render.yaml` is at the top level of the repository and `backend/models/tomato/model.onnx` exists.
   (Browser uploads allow files up to 25 MB; the models are usually about 16 to 22 MB.)

## Step 2: Start the server on Render (about 10 minutes)

1. Go to https://render.com, click **Get started**, and sign up **with GitHub**.
2. Click **New → Blueprint**, connect your GitHub account if asked, and pick `zemli-app`.
   Render reads `render.yaml`: one free web service called `zemli`. Click **Apply** / **Deploy Blueprint**.
3. Open the `zemli` service and watch **Logs**. The first build takes about 5 to 10 minutes.
   When you see `Application startup complete` and `models loaded for: ['cucumber', 'gate', 'tomato']`, it's live.
4. The address is at the top of the service page, for example `https://zemli.onrender.com`
   (Render may add letters, like `https://zemli-ab12.onrender.com`).
   - App: `https://zemli.onrender.com/app`
   - Admin: `https://zemli.onrender.com/admin`
5. Your admin key: service → **Environment** → `ZEMLI_API_KEY` → click the eye icon. Keep it private.

Open the `/app` address on your phone and log in with `9000000001` to check it works.

### What the free plan means
- **It sleeps after 15 minutes with no visitors.** The next visitor waits about a minute while it wakes up.
- **Data resets every time it sleeps or restarts.** Farmers, scans and field photos your team adds disappear;
  the demo data (employees 9000000001 / 9000000002, farmers 9000000100 to 9000000119) loads again automatically.
  Best used for testing sessions where the team tests together. Never enter real farmers' Aadhaar or bank details.
- 512 MB memory: enough for the three AI models (tested at about 270 MB).
- For the real pilot (data kept, no sleeping) use a paid server: see `DEPLOY.md`.

## Step 3: Make the APK with PWABuilder (about 10 minutes)

1. Open the app address once so the server is awake, then go to https://www.pwabuilder.com,
   enter `https://zemli.onrender.com/app` (your address) and click **Start**.
   Yellow warnings such as "screenshots" or "categories" are optional; ignore them.
2. **Package for stores → Android → Generate package**:
   - Package ID: `in.zemliagritech.app`
   - App name `Zemli Agritech`, short name `Zemli`
   - Display mode: Standalone
   - Location delegation: **on** (field photos and scans use GPS)
   - Signing key: **Create new**
3. Download the zip. Inside:
   - `*.apk`: install this on phones
   - `signing.keystore` and `signing-key-info.txt`: **keep these safe** (Google Drive, private). Every future update needs them.
   - `assetlinks.json`
4. Open `assetlinks.json` in Notepad and copy everything. On Render: service → **Environment** →
   **Add environment variable**: key `ASSETLINKS_JSON`, value = the copied text → **Save changes** (it redeploys).
   This makes the app open full screen without a browser bar.
5. Install the APK on your own phone first and test a login, a crop scan and a daily field photo.

## Step 4: Send it to the team

Share the `.apk` on WhatsApp or Google Drive with this note:

> Zemli test app: download the APK and tap it. If Android asks, allow "Install unknown apps". If Play Protect
> warns about an unknown developer, tap "More details → Install anyway". If the app takes a minute to open, the
> test server is waking up. Log in with 9000000001 (employee) or 9000000102 (farmer); the OTP shows on screen in
> test mode. Allow camera and location.

iPhone users can't install APKs: they open the `/app` link in Safari → Share → **Add to Home Screen**.

## Updating later

Upload changed files to the GitHub repository (or replace them) and Render rebuilds automatically.
Everyone's installed app shows the new version; you only build a new APK if you change the app's name or icon.
