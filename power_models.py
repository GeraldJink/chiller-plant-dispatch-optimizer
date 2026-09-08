"""Fictional chiller, affinity-law pumps/fans and normalized Merkel-NTU tower."""
import math


def chiller_cop(plr, chw_supply_c, cw_return_c, model, multiplier=1.0):
    lift_offset = (cw_return_c - chw_supply_c) - (model["reference_cw_return_c"] - model["reference_chw_supply_c"])
    cop = model["peak_cop"] * multiplier / (1 + model["part_load_curvature"] * (plr - model["peak_plr"]) ** 2)
    cop *= math.exp(max(-50, min(50, -model["lift_sensitivity_per_c"] * lift_offset)))
    return max(model["minimum_cop"], cop)


def affinity_power(rated_power_kw, speed_ratio):
    return rated_power_kw * speed_ratio ** 3


def affinity_flow(rated_flow_m3_h, speed_ratio):
    return rated_flow_m3_h * speed_ratio


def affinity_head(rated_head_m, speed_ratio):
    return rated_head_m * speed_ratio ** 2


def merkel_approach(range_c, air_fraction, model):
    """Toy NTU calibration at each water range; not a fitted Merkel integral."""
    if air_fraction <= 0:
        return math.inf
    ntu_full = math.log1p(range_c / model["full_speed_approach_c"])
    return range_c / math.expm1(ntu_full * air_fraction ** model["air_exponent"])


def required_air_fraction(range_c, approach_c, model):
    return (math.log1p(range_c / approach_c) / math.log1p(range_c / model["full_speed_approach_c"])) ** (1 / model["air_exponent"])
