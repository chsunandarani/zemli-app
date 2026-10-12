"""Pretend to be Zemli IoT boxes, so you can demo the sensors without hardware.

1. Admin panel -> IoT devices -> register a box (e.g. ZM-0001) for a farmer, copy its key.
2. python simulate_iot.py --server http://localhost:8000 --device ZM-0001 --key THE_KEY
   Add --dry to send low soil moisture, which triggers the Telugu alert in the farmer's app.
"""
import argparse, json, math, random, time
from urllib.request import Request, urlopen

ap = argparse.ArgumentParser()
ap.add_argument("--server", default="http://localhost:8000")
ap.add_argument("--device", required=True)
ap.add_argument("--key", required=True)
ap.add_argument("--every", type=float, default=10, help="seconds between readings (a real box uses 900)")
ap.add_argument("--count", type=int, default=0, help="stop after N readings (0 = run until Ctrl+C)")
ap.add_argument("--dry", action="store_true", help="simulate dry soil")
a = ap.parse_args()

i = 0
while True:
    t = time.time() / 3600
    reading = {
        "soil_moisture": round((20 if a.dry else 50) + 8 * math.sin(t) + random.uniform(-3, 3), 1),
        "air_temp": round(29 + 4 * math.sin(t / 2) + random.uniform(-1, 1), 1),
        "humidity": round(65 + random.uniform(-8, 8)),
        "nitrogen": round(random.uniform(45, 90)), "phosphorus": round(random.uniform(18, 40)), "potassium": round(random.uniform(110, 220)),
    }
    req = Request(f"{a.server}/api/iot/reading", data=json.dumps(reading).encode(), method="POST",
                  headers={"Content-Type": "application/json", "X-Device-Id": a.device, "X-Device-Key": a.key})
    try:
        out = json.loads(urlopen(req, timeout=15).read())
        print(reading, "->", out["status"]["light"], "(alert sent)" if out["alert_sent"] else "")
    except Exception as e:
        print("failed:", e)
    i += 1
    if a.count and i >= a.count:
        break
    time.sleep(a.every)
