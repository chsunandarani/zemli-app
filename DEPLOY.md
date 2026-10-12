# Putting Zemli online permanently

Goal: the app works any time, from anywhere, without your laptop, at an address like
`https://app.zemliagritech.in/field`, and field staff install it on their phones.

## 1. Get a small Linux server

Any Ubuntu 22.04/24.04 server with 2 GB RAM and 25+ GB disk is enough for the pilot
(for example DigitalOcean, AWS Lightsail, Hetzner, or Oracle Cloud's free tier).
Note its public IP address.

Avoid free hosting that wipes files on restart (many free "web service" plans do).
The database and uploaded photos must survive restarts.

## 2. Point your domain at it

In GoDaddy: My Products → zemliagritech.in → DNS → Add record:
- Type `A`, Name `app`, Value = the server's IP, TTL default.

After 5-30 minutes, `app.zemliagritech.in` points to your server.

## 3. Install the backend on the server

Connect with SSH (`ssh root@YOUR_IP`), then:

```bash
apt update && apt install -y python3-venv unzip caddy
mkdir -p /opt/zemli && cd /opt/zemli
# upload zemli-prototype.zip from your laptop first, e.g.:  scp zemli-prototype.zip root@YOUR_IP:/opt/zemli/
unzip zemli-prototype.zip
cd zemli/backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Copy your trained models from the laptop into `/opt/zemli/zemli/backend/models/`
(the `tomato`, `cucumber` and `gate` folders), for example with
`scp -r backend\models\* root@YOUR_IP:/opt/zemli/zemli/backend/models/`.

## 4. Run it as a service (starts on boot, restarts if it crashes)

Create `/etc/systemd/system/zemli.service`:

```ini
[Unit]
Description=Zemli backend
After=network.target

[Service]
WorkingDirectory=/opt/zemli/zemli/backend
Environment=ZEMLI_API_KEY=CHANGE-THIS-TO-A-LONG-RANDOM-SECRET
ExecStart=/opt/zemli/zemli/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Then: `systemctl daemon-reload && systemctl enable --now zemli && systemctl status zemli`

Use a long random key (for example from https://www.random.org/strings/) and give it only to staff.

## 5. HTTPS with Caddy

Replace `/etc/caddy/Caddyfile` with:

```
app.zemliagritech.in {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 12MB
    }
}
```

Then `systemctl reload caddy`. Caddy gets and renews the HTTPS certificate automatically.
Open `https://app.zemliagritech.in` (dashboard) and `https://app.zemliagritech.in/field` (field app).

## 6. Install on phones

- **Android (Chrome):** open `https://app.zemliagritech.in/field`, tap **Install app** at the top
  (or Chrome menu → Install app / Add to Home screen). It gets the Zemli icon and opens full screen.
- **iPhone (Safari):** Share → Add to Home Screen.

## 7. Optional: a real APK / Play Store app without Flutter

Go to https://www.pwabuilder.com, enter `https://app.zemliagritech.in/field`, choose **Android**,
and download the package. It contains an APK you can install directly for testing, and an
AAB file for the Google Play Store (needs a Play developer account). It runs the same web app,
so every update you make on the server reaches all phones without a new APK.

## Backups

Everything that matters is in `/opt/zemli/zemli/backend/`: `zemli.db`, `uploads/` and `models/`.
Copy them off the server regularly, for example once a week.

## Updating the code later

Upload the new zip, unzip over `/opt/zemli` (models, database and uploads are not in the zip, so
they stay), then `systemctl restart zemli`.
