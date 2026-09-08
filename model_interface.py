"""Stable pointwise chiller power interface shared by search and validation."""
from dataclasses import dataclass
from importlib import import_module
from copy import deepcopy
import math
import re

from power_models import chiller_cop


@dataclass(frozen=True)
class ChillerInputs:
    load_kw: float
    capacity_kw: float
    chw_supply_c: float
    chw_return_c: float
    cw_supply_c: float
    cw_return_c: float
    wet_bulb_c: float

    @property
    def plr(self):
        return self.load_kw / self.capacity_kw


INPUT_NAMES = (*ChillerInputs.__dataclass_fields__, "plr")


class ModelDomainError(ValueError):
    """This operating point is outside the model's valid region, not a code error."""


def validate_predictor(spec):
    if not isinstance(spec, dict) or set(spec) - {"factory", "options", "domain"}:
        raise ValueError("predictor: expected factory, options and domain")
    factory = spec.get("factory")
    if not isinstance(factory, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("predictor.factory must be module.path:factory_name")
    if not isinstance(spec.get("options", {}), dict):
        raise ValueError("predictor.options must be an object")
    domain = spec.get("domain")
    if not isinstance(domain, dict) or not {"plr", "chw_supply_c", "cw_return_c"} <= set(domain) or set(domain) - set(INPUT_NAMES):
        raise ValueError("predictor.domain must include plr, chw_supply_c, cw_return_c and only known inputs")
    for name, bounds in domain.items():
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds) or bounds[0] > bounds[1]:
            raise ValueError(f"predictor.domain.{name}: expected finite [min, max]")


class ChillerPowerModel:
    def __init__(self, cfg, device):
        self.device_id = device["id"]
        self.domain = {}
        spec = device.get("predictor")
        if spec is None:
            model = {**cfg["chiller_model"], **device.get("model", {})}
            multiplier = device["cop_multiplier"]
            self.predict = lambda x: x.load_kw / chiller_cop(x.plr, x.chw_supply_c, x.cw_return_c, model, multiplier)
        else:
            validate_predictor(spec)
            self.domain = deepcopy(spec["domain"])
            module, factory = spec["factory"].split(":")
            try:
                self.predict = getattr(import_module(module), factory)(deepcopy(spec.get("options", {})))
                if not callable(self.predict):
                    raise TypeError("Factory must return a callable taking ChillerInputs")
            except Exception as exc:
                raise ValueError(f"{self.device_id}: cannot load predictor {spec['factory']}: {exc}") from exc

    def power_kw(self, inputs):
        for name, (low, high) in self.domain.items():
            if not low - 1e-9 <= getattr(inputs, name) <= high + 1e-9:
                raise ModelDomainError(f"{self.device_id}: {name} outside model domain")
        try:
            value = self.predict(inputs)
            if isinstance(value, (bool, str, bytes)):
                raise TypeError("Power must be a numeric scalar")
            power = float(value)
            if not math.isfinite(power) or power <= 0:
                raise ValueError("Running chiller power must be finite and positive kW")
            return power
        except ModelDomainError:
            raise
        except Exception as exc:
            raise ValueError(f"{self.device_id}: invalid model prediction: {exc}") from exc


def load_chiller_models(cfg):
    """Load each device's artifact once per optimization, never per fitness call."""
    return {d["id"]: ChillerPowerModel(cfg, d) for d in cfg["chillers"]}
