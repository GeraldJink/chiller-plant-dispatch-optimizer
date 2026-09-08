"""Portable load CSV contract and deterministic synthetic data generation."""
import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

LOAD_COLUMNS = ("timestamp", "load_kw", "wet_bulb_c", "duration_hours")


def validate_load(rows):
    if not rows:
        raise ValueError("Load data is empty")
    previous_end = None
    for index, row in enumerate(rows, 2):
        try:
            stamp = datetime.fromisoformat(row["timestamp"])
            load, wet, duration = (float(row[key]) for key in LOAD_COLUMNS[1:])
            if not all(math.isfinite(v) for v in (load, wet, duration)):
                raise ValueError("All values must be finite")
            if load < 0 or not -30 <= wet <= 45 or duration <= 0:
                raise ValueError("Require load >= 0, wet bulb in [-30,45], duration > 0")
            if previous_end is not None and abs((stamp - previous_end).total_seconds()) > 0.001:
                raise ValueError("Timestamps must be increasing and contiguous with duration_hours")
            previous_end = stamp + timedelta(hours=duration)
            row.update(timestamp=stamp.isoformat(), load_kw=load, wet_bulb_c=wet, duration_hours=duration)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Load CSV row {index}: {exc}") from exc
    return rows


def read_load_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(reader.fieldnames) != set(LOAD_COLUMNS):
            raise ValueError(f"CSV header must contain exactly: {','.join(LOAD_COLUMNS)}")
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValueError("CSV row has missing or extra fields")
    return validate_load(rows)


def write_load_csv(path, rows):
    validate_load(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOAD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def generate_load(cfg):
    gen = cfg["load_generation"]
    rng = random.Random(gen["seed"])
    start = datetime.fromisoformat(gen["start"])
    capacity = sum(d["capacity_kw"] * d["max_plr"] for d in cfg["chillers"])
    low, high = gen["min_load_fraction"], gen["max_load_fraction"]
    current = (low + high) / 2
    rows = []
    for i in range(gen["periods"]):
        stamp = start + timedelta(minutes=i * gen["interval_minutes"])
        hour = stamp.hour + stamp.minute / 60
        wave = (1 + math.sin((hour - 8) * math.pi / 12)) / 2
        target = low + (high - low) * wave + rng.uniform(-0.03, 0.03)
        step = gen["max_load_step_fraction"]
        current = max(low, min(high, max(current - step, min(current + step, target))))
        wet = gen["wet_bulb_min_c"] + (gen["wet_bulb_max_c"] - gen["wet_bulb_min_c"]) * wave
        rows.append({"timestamp": stamp.isoformat(), "load_kw": round(current * capacity, 3), "wet_bulb_c": round(wet, 3), "duration_hours": gen["interval_minutes"] / 60})
    return validate_load(rows)
