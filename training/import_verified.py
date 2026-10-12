"""Pull agronomist-verified field photos from the backend into the training folders.

    python import_verified.py --server http://localhost:8000 --key YOURKEY --dest data

Photos land in data/<crop>/field_verified/<label>/. Move a fixed share of them into
test_field/ BEFORE training and never train on those, so the field score stays honest.
"""
import argparse
import csv
import io
from pathlib import Path
from urllib.request import Request, urlopen


def get(url, key):
    return urlopen(Request(url, headers={"X-Api-Key": key} if key else {})).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--key", default="")
    ap.add_argument("--dest", default="data")
    a = ap.parse_args()
    rows = list(csv.DictReader(io.StringIO(get(f"{a.server}/api/export/verified.csv", a.key).decode())))
    new = 0
    for r in rows:
        if r["final_label"] == "unclear_photo":
            continue
        target = Path(a.dest) / r["crop"] / "field_verified" / r["final_label"] / Path(r["image_path"]).name
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(get(f"{a.server}/uploads/{r['image_path']}", a.key))
        new += 1
    print(f"{len(rows)} verified scans on server, {new} new photos downloaded")


if __name__ == "__main__":
    main()
