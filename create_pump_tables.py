"""Optional synthetic affinity-law pump table export; not required by the planner."""
import argparse
import csv
from pathlib import Path
from config import load_config
from pump_lookup import PumpLookupTable


def create_pump_lookup_tables(cfg, directory, points=101):
    if type(points) is not int or points < 2:
        raise ValueError("points must be an integer >= 2")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for group in ("chw_pumps", "cw_pumps"):
        bank = PumpLookupTable(cfg[group])
        maximum = sum(d["flow_m3_h"] * d["max_speed_ratio"] for d in cfg[group])
        with (directory / f"{group}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("flow_m3_h", "feasible", "power_kw", "device_ids", "speed_ratio"))
            for i in range(points):
                flow = maximum * i / (points - 1)
                result = bank.lookup(flow)
                writer.writerow((flow, result is not None, result["power_kw"] if result else "", ";".join(d["id"] for d in result["devices"]) if result else "", result["devices"][0]["speed_ratio"] if result and result["devices"] else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--output", default="outputs/pump_tables")
    parser.add_argument("--points", type=int, default=101)
    args = parser.parse_args()
    try:
        create_pump_lookup_tables(load_config(args.config), args.output, args.points)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
