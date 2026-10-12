# Share Zemli with your team: permanent link + Android APK

An APK of this app is a wrapper around the web app, so the server must always be reachable at a
fixed address. Step 1 gives you that address for free. Step 2 turns it into an APK.

## Step 1: Free permanent link on Hugging Face Spaces (about 20 minutes)

1. Make a free account at https://huggingface.co.
2. Click your profile picture → **New Space**.
   - Space name: `zemli`
   - SDK: **Docker**, template **Blank**
   - Hardware: **CPU basic (free)**
   - Visibility: **Public** (a private Space asks every visitor to log in to Hugging Face, which breaks the app). The app itself still needs phone + OTP login, and the admin panel needs your key.
3. In the new Space, open **Files → Add file → Upload files**. From `D:\zemli-prototype\zemli\backend`, drag in:
   - `Dockerfile`, `.dockerignore`, `requirements.txt`
   - the `app` folder
   - the `models` folder (your trained tomato, cucumber and gate models)

   Do **not** upload `.venv`, `zemli.db`, `uploads` or `start.ps1`. Click **Commit changes**.
4. Open **Settings → Variables and secrets → New secret**: name `ZEMLI_API_KEY`, value = a long random key. Restart the Space if it asks.
5. Watch the **Logs**. When the status turns **Running**, your app is at:
   - App: `https://YOUR-USERNAME-zemli.hf.space/app`
   - Admin: `https://YOUR-USERNAME-zemli.hf.space/admin`

   (Replace YOUR-USERNAME with your Hugging Face username. The **Embed this Space / direct URL** option in the Space menu shows the exact address.)

What to know about the free Space:
- It runs without your laptop.
- If nobody uses it for about 2 days it goes to sleep; the first visit then takes a minute to wake it.
- **Data resets whenever it restarts.** The demo data (employee 9000000001, farmers 9000000100-119) is loaded again automatically. This is for team testing only: never put real farmers' Aadhaar or bank details here.
- For the real trial, use a paid server with a permanent disk (see `DEPLOY.md`).

## Step 2: Make the APK with PWABuilder (about 10 minutes)

1. Open https://www.pwabuilder.com and enter `https://YOUR-USERNAME-zemli.hf.space/app`. Click **Start**.
   It checks the app's manifest, icons and offline support (all included).
2. Click **Package For Stores → Android → Generate Package**. In the options:
   - Package ID: `in.zemliagritech.app`
   - App name: `Zemli Agritech`, short name `Zemli`
3. Download the zip. Inside you'll find:
   - `*.apk`: install this on phones for testing
   - `*.aab`: only needed for the Google Play Store later
   - `signing.keystore` and `signing-key-info.txt`: **keep these safe** (Google Drive, private). You need the same key for every future update.
   - `assetlinks.json`
4. Open `assetlinks.json` in Notepad, copy everything, and in your Space go to **Settings → Variables and secrets → New variable**: name `ASSETLINKS_JSON`, value = the copied text. Restart the Space.
   This lets the APK open full screen without a browser address bar.

## Step 3: Send it to the team

Send the `.apk` file on WhatsApp or Google Drive with this note:

> Zemli test app: download the APK and tap it. If Android asks, allow "Install unknown apps" for WhatsApp/Files. If Play Protect warns about an unknown developer, tap "More details → Install anyway". Log in with 9000000001 (employee view) or 9000000102 (farmer view); the OTP shows on screen in test mode. Allow camera and location.

iPhone users can't install APKs: they open the app link in Safari → Share → **Add to Home Screen**.

Every change you upload to the Space reaches everyone's APK automatically; you only build a new APK if you change the app name, icon or package details.
