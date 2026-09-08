"""Run the fictional mechanistic adapter through the real optimizer without extra packages."""
import argparse
from config import load_config
from ga_series import SeriesGA
from load_generate import generate_load
from main import save_result


def demo_config():
    cfg = load_config()
    # Each device may select a different factory and a different trained artifact.
    for device in cfg["chillers"]:
        device["predictor"] = {
            "factory": "examples.model_adapters:create_mechanistic",
            "options": {"efficiency": 0.6},
            "domain": {"plr": [0.15, 1.0], "chw_supply_c": [6, 12], "cw_return_c": [23, 42]},
        }
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="outputs/custom_model")
    args = parser.parse_args()
    cfg = demo_config()
    loads = generate_load(cfg)
    result = SeriesGA(cfg).optimize(loads)
    save_result(args.output, cfg, loads, result)
    print(f"Custom mechanistic model: {result['total_energy_kwh']:.2f} kWh; results in {args.output}")


if __name__ == "__main__":
    main()
