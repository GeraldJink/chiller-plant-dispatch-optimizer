"""JSON configuration and validation; no site connections or private defaults."""
import json
import math
from pathlib import Path
from datetime import datetime
from model_interface import validate_predictor
from load_allocation import validate_allocation_config
from chiller_scheduling import validate_scheduling_config

DEFAULT_CONFIG = Path(__file__).parent / "config" / "default.json"
CONTROLS = ("chw_supply_c", "chw_delta_c", "cw_delta_c", "cw_supply_c")
TEMPERATURES = ("chw_supply_c", "chw_return_c", "cw_supply_c", "cw_return_c")


def load_config(path=None):
    with Path(path or DEFAULT_CONFIG).open(encoding="utf-8-sig") as handle:
        cfg = json.load(handle)
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def number(value, name, minimum=None, maximum=None):
        require(type(value) in (int, float) and math.isfinite(value), f"{name}: finite number required")
        require(minimum is None or value >= minimum, f"{name}: below minimum {minimum}")
        require(maximum is None or value <= maximum, f"{name}: above maximum {maximum}")

    try:
        with DEFAULT_CONFIG.open(encoding="utf-8") as handle:
            template = json.load(handle)

        def known_keys(value, reference, path):
            if isinstance(reference, dict):
                require(isinstance(value, dict), f"{path}: expected object")
                extra = set(value) - set(reference)
                if path.startswith("chillers["):
                    extra.discard("model")
                    extra.discard("predictor")
                require(not extra, f"{path}: unknown fields {sorted(extra)}")
                for key in value:
                    if key in reference and value[key] is not None:
                        known_keys(value[key], reference[key], f"{path}.{key}" if path else key)
            elif isinstance(reference, list) and reference and isinstance(reference[0], dict):
                require(isinstance(value, list), f"{path}: expected list")
                for i, item in enumerate(value):
                    known_keys(item, reference[0], f"{path}[{i}]")

        known_keys(cfg, template, "")
        require(cfg["schema_version"] == 1, "Unsupported schema_version")
        number(cfg["water_heat_capacity_kwh_m3_k"], "water heat capacity", 0.001)
        identifiers = set()
        for group in ("chillers", "chw_pumps", "cw_pumps", "towers"):
            devices = cfg[group]
            require(isinstance(devices, list) and 1 <= len(devices) <= 8, f"{group}: provide 1..8 devices")
            for device in devices:
                name = device["id"]
                require(isinstance(name, str) and name.strip() and name not in identifiers, "Device IDs must be unique nonempty strings")
                identifiers.add(name)
                fields = {"chillers": ("capacity_kw", "cop_multiplier"), "chw_pumps": ("flow_m3_h", "power_kw", "head_m", "rated_hz"), "cw_pumps": ("flow_m3_h", "power_kw", "head_m", "rated_hz"), "towers": ("heat_rejection_kw", "fan_power_kw", "rated_hz")}[group]
                for field in fields:
                    number(device[field], f"{name}.{field}", 0.001)
                lo, hi = ("min_plr", "max_plr") if group == "chillers" else ("min_speed_ratio", "max_speed_ratio")
                number(device[lo], f"{name}.{lo}", 0.001, 1)
                number(device[hi], f"{name}.{hi}", device[lo], 1)
        validate_scheduling_config(cfg)
        validate_allocation_config(cfg.get("load_allocation"))
        model = cfg["chiller_model"]
        for field in ("peak_cop", "part_load_curvature", "lift_sensitivity_per_c", "minimum_cop"):
            number(model[field], field, 0.00001)
        number(model["peak_plr"], "peak_plr", 0.01, 1)
        for field in ("reference_chw_supply_c", "reference_cw_return_c"):
            number(model[field], field)
        require(model["minimum_cop"] < model["peak_cop"], "minimum_cop must be below peak_cop")
        for device in cfg["chillers"]:
            if "predictor" in device:
                validate_predictor(device["predictor"])
            overrides = device.get("model", {})
            require(isinstance(overrides, dict) and not (set(overrides) - set(model)), f"{device['id']}.model: unknown fields")
            effective = {**model, **overrides}
            for field, value in effective.items():
                number(value, f"{device['id']}.model.{field}")
            for field in ("peak_cop", "part_load_curvature", "lift_sensitivity_per_c", "minimum_cop"):
                require(effective[field] > 0, f"{device['id']}.model.{field} must be positive")
            require(0 < effective["peak_plr"] <= 1 and effective["minimum_cop"] < effective["peak_cop"], f"{device['id']}: invalid COP model")
        tower = cfg["tower_model"]
        number(tower["full_speed_approach_c"], "full_speed_approach_c", 0.01)
        number(tower["maximum_approach_c"], "maximum_approach_c", tower["full_speed_approach_c"])
        number(tower["air_exponent"], "air_exponent", 0.01)
        opt = cfg["optimization"]
        bounds = opt["bounds"]
        for field in (*CONTROLS, "chw_return_c", "cw_return_c", "approach_c"):
            pair = bounds[field]
            require(isinstance(pair, list) and len(pair) == 2, f"{field}: expected [min, max]")
            number(pair[0], field)
            number(pair[1], field, pair[0])
        for field in ("chw_delta_c", "cw_delta_c"):
            require(bounds[field][0] > 0, f"{field} must be positive")
        require(tower["full_speed_approach_c"] <= bounds["approach_c"][0] <= bounds["approach_c"][1] <= tower["maximum_approach_c"], "Approach bounds must lie within tower model limits")
        for field in ("max_temperature_step_c", "max_control_step_c"):
            number(opt[field], field, 0.0001)
        for field, minimum in (("population_size", 4), ("generations", 1), ("elite_count", 1), ("tournament_size", 2), ("seed", 0)):
            require(type(opt[field]) is int and opt[field] >= minimum, f"{field}: integer >= {minimum} required")
        require(opt["elite_count"] < opt["population_size"], "elite_count must be smaller than population_size")
        require(opt["tournament_size"] <= opt["population_size"], "tournament_size exceeds population_size")
        for field in ("crossover_rate", "mutation_rate", "mutation_scale"):
            number(opt[field], field, 0, 1)
        initial = opt["initial_temperatures"]
        if initial is not None:
            for field in TEMPERATURES:
                number(initial[field], f"initial.{field}", *bounds[field])
            for prefix in ("chw", "cw"):
                number(initial[f"{prefix}_return_c"] - initial[f"{prefix}_supply_c"], f"initial.{prefix}_delta_c", *bounds[f"{prefix}_delta_c"])
        gen = cfg["load_generation"]
        datetime.fromisoformat(gen["start"])
        for field in ("periods", "interval_minutes", "seed"):
            require(type(gen[field]) is int and gen[field] >= (0 if field == "seed" else 1), f"load_generation.{field}: invalid integer")
        number(gen["min_load_fraction"], "min_load_fraction", 0, 1)
        number(gen["max_load_fraction"], "max_load_fraction", gen["min_load_fraction"], 1)
        number(gen["max_load_step_fraction"], "max_load_step_fraction", 0, 1)
        number(gen["wet_bulb_min_c"], "wet_bulb_min_c", -30, 45)
        number(gen["wet_bulb_max_c"], "wet_bulb_max_c", gen["wet_bulb_min_c"], 45)
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"Invalid/missing configuration field: {exc}") from exc
