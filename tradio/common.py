"""Shared helpers for Tradio's data scripts (role 1)."""

import json
import os
import re
from datetime import datetime, timezone

import yaml

TRADIO_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TRADIO_DIR)
DATA_DIR = os.path.join(TRADIO_DIR, "data")
WEB_DATA_DIR = os.path.join(ROOT, "web", "data", "tradio")
EVENTS_PATH = os.path.join(TRADIO_DIR, "events.yaml")


def load_events() -> dict:
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_ts(value: str) -> datetime:
    """'2025-06-23T14:08:43.49459Z' -> aware UTC datetime. Kalshi sends 0-6 fractional
    digits (e.g. .346, .49459), which Python < 3.11 can't parse, so pad to 6."""
    text = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", text)
    if m:
        frac = (m.group(2) or "")[:6]
        text = m.group(1) + ("." + frac.ljust(6, "0") if frac else "") + (m.group(3) or "")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_unix(value: str) -> int:
    return int(parse_ts(value).timestamp())


def iso(dt: datetime) -> str:
    """UTC ISO 8601 with Z; keeps microseconds only when present."""
    dt = dt.astimezone(timezone.utc)
    text = dt.isoformat().replace("+00:00", "Z")
    return text


def iso_from_unix(seconds: int) -> str:
    return iso(datetime.fromtimestamp(int(seconds), tz=timezone.utc))


def raw_dir(event_id: str) -> str:
    path = os.path.join(DATA_DIR, event_id, "raw")
    os.makedirs(path, exist_ok=True)
    return path


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
