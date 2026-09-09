"""Run an offline plant plan from a CSV file or a generated load curve."""
import argparse
import csv
import json
import math
import sys
from pathlib import Path
from config import load_config, validate_config
from ga_series import SeriesGA
from load_generate import generate_load, read_load_csv, write_load_csv


def save_result(output, cfg, loads, result):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_load_csv(output / "load.csv", loads)
    for name, value in (("result.json", result), ("config.json", cfg)):
        with (output / name).open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    schedule = result["schedule"]
    with (output / "schedule.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(schedule[0]))
        writer.writeheader()
        for row in schedule:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for k, v in row.items()})
    with (output / "convergence.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("generation", "best_energy_kwh"))
        writer.writeheader()
        writer.writerows(result["history"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline genetic-algorithm chiller plant planner (fictional models)")
    parser.add_argument("--config", help="Plant JSON; defaults to config/default.json")
    parser.add_argument("--load", help="Load CSV; omit to generate a reproducible synthetic curve")
    parser.add_argument("--runtime-hours", type=float, nargs="+", help="Cumulative chiller run hours in config device order; overrides chiller_scheduling.runtime_hours")
    parser.add_argument("--output", default="outputs/demo", help="Result directory (default: outputs/demo)")
    parser.add_argument("--generate-only", metavar="CSV", help="Write generated load CSV and exit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.runtime_hours is not None:
            cfg.setdefault("chiller_scheduling", {})["runtime_hours"] = args.runtime_hours
            validate_config(cfg)
        if args.generate_only:
            if args.load:
                parser.error("--generate-only cannot be combined with --load")
            path = Path(args.generate_only).resolve()
            if path.exists():
                raise ValueError(f"Refusing to overwrite existing load file: {path}")
            write_load_csv(path, generate_load(cfg))
            print(f"Generated load: {path}")
            return 0
        output = Path(args.output).resolve()
        for source in (args.config, args.load):
            if source and Path(source).resolve() in {output / name for name in ("load.csv", "config.json", "result.json", "schedule.csv", "convergence.csv")}:
                raise ValueError("Choose an output directory that does not overwrite the input config/load")
        loads = read_load_csv(args.load) if args.load else generate_load(cfg)

        def progress(generation, energy):
            if not args.quiet and (generation % 5 == 0 or generation == cfg["optimization"]["generations"]):
                value = f"{energy:.2f} kWh" if math.isfinite(energy) else "searching for feasible schedule"
                print(f"Generation {generation}: {value}", flush=True)

        result = SeriesGA(cfg).optimize(loads, progress=progress)
        save_result(output, cfg, loads, result)
        print(f"Feasible best-found schedule: {result['total_energy_kwh']:.2f} kWh; {len(loads)} periods")
        print(f"Results: {output}")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
