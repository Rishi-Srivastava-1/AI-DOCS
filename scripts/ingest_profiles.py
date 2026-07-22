#!/usr/bin/env python3
"""
Bulk profile ingestion script for CleverTap LC endpoint.
Uses only stdlib — no pip install needed.

Usage:
    python3 ai_docs/scripts/ingest_profiles.py
"""

import json
import random
import string
import time
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Edit these to change behaviour
# ---------------------------------------------------------------------------

ACCOUNT_ID  = "WWW-WWW-WWRZ"
TOKEN       = "000-000"
HOST        = "http://localhost:8081/a1?t=30700&os=Android"
COUNT       = 10000           # total profiles to generate
DELAY_MS    = 100         # ms to wait between requests (0 = no delay)
DRY_RUN     = False       # True = print payload, skip network

EVENT_NAME  = "ev7"
EVENT_DATA  = {
    "cs-ep":  "129.01",
    "new-ep": "SC-PS4-GTSPORTS7-L",
    "key1":   "value1",
}

# ---------------------------------------------------------------------------
# Device info (af)
# ---------------------------------------------------------------------------

DEVICE_INFO = {
    "Build": "70700",
    "Version": "7.7.0",
    "OS Version": "15",
    "SDK Version": 70700,
    "Make": "Google",
    "Model": "sdk_gphone64_arm64",
    "Carrier": "T-Mobile",
    "useIP": False,
    "OS": "Android",
    "wdt": 2.2,
    "hgt": 5.36,
    "dpi": 420,
    "dt": 1,
    "locale": "en_US",
    "abckt": "active",
    "cc": "us",
    "LIAMC": 0,
}

ARP = {
    "wdt": 2.3,
    "d_ts": 0,
    "id": "WWW-WWW-WWRZ",
    "hgt": 4.09,
    "dh": 0,
    "v": 1,
    "j_s": "{ }",
}

UPLOAD_PATH = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIRST_NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve", "Frank", "Grace", "Hank"]
LAST_NAMES  = ["Smith", "Johnson", "Lee", "Kumar", "Wang", "Garcia", "Brown", "Patel"]
CITIES      = ["Mumbai", "Delhi", "Bangalore", "Hyderabad", "Chennai", "Kolkata", "Pune"]
GENDERS     = ["M", "F"]
PLANS       = ["free", "pro", "enterprise"]


def _rand(k: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


def _new_guid() -> str:
    return f"__guid{_rand(10)}"


def _profile() -> dict[str, Any]:
    first = random.choice(FIRST_NAMES)
    last  = random.choice(LAST_NAMES)
    return {
        "type": "profile",
        "profile": {
            "Name":   f"{first} {last}",
            "Gender": random.choice(GENDERS),
            "City":   random.choice(CITIES),
            "Plan":   random.choice(PLANS),
        },
    }


def _event() -> dict[str, Any]:
    return {
        "type":    "event",
        "evtName": EVENT_NAME,
        "evtData": EVENT_DATA,
    }


def _meta(guid: str) -> dict[str, Any]:
    return {
        "g":       guid,
        "arp":     ARP,
        "af":      DEVICE_INFO,
        "id":      ACCOUNT_ID,
        "tk":      TOKEN,
        "imp":     0,
        "type":    "meta",
        "regURLs": ["https://www.mensxp.com", "ademo", "com.clevertap.sample", "https", "brb"],
        "tlc":     [],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def send(guid: str) -> bool:
    url     = HOST.rstrip("/") + UPLOAD_PATH
    payload = json.dumps([_meta(guid), _profile(), _event()], separators=(",", ":")).encode("utf-8")

    if DRY_RUN:
        print(f"  [DRY RUN] POST {url} | payload: {payload[:150].decode()}...")
        return True

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  -> {resp.status} | {resp.read().decode()[:120]}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  -> HTTP {e.code}: {e.read().decode()[:120]}")
        return False
    except urllib.error.URLError as e:
        print(f"  -> ERROR: {e.reason}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Ingesting {COUNT} profiles")
    print(f"Target: {HOST}")
    if DRY_RUN:
        print("Mode: DRY RUN (no requests sent)")
    print()

    success = 0
    for i in range(COUNT):
        guid = _new_guid()
        print(f"[{i + 1}/{COUNT}] GUID: {guid}")
        if send(guid):
            success += 1
        if DELAY_MS > 0:
            time.sleep(DELAY_MS / 1000)

    print(f"\nDone. {success}/{COUNT} succeeded.")


if __name__ == "__main__":
    main()
