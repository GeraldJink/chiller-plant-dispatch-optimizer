"""Stateless, replaceable per-combination cooling-load allocation in kW."""
from dataclasses import dataclass
from copy import deepcopy
from importlib import import_module
from collections.abc import Mapping
import math
import re


class AllocationInfeasible(ValueError):
    """The allocator cannot meet this combination's load and PLR constraints."""


@dataclass(frozen=True)
class AllocationChiller:
    id: str
    capacity_kw: float
    min_plr: float
    max_plr: float


@dataclass(frozen=True)
class AllocationInputs:
    load_kw: float
    chillers: tuple
    chw_supply_c: float
    chw_return_c: float
    cw_supply_c: float
    cw_return_c: float
    wet_bulb_c: float


def validate_allocation_config(spec):
    if spec is None:
        return
    if not isinstance(spec, dict) or set(spec) - {"factory", "options"}:
        raise ValueError("load_allocation: expected factory and options")
    factory = spec.get("factory")
    if not isinstance(factory, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("load_allocation.factory must be module.path:factory_name")
    if not isinstance(spec.get("options", {}), dict):
        raise ValueError("load_allocation.options must be an object")


def capacity_weighted(inputs):
    capacity = sum(d.capacity_kw for d in inputs.chillers)
    return {d.id: inputs.load_kw * d.capacity_kw / capacity for d in inputs.chillers}


class LoadAllocator:
    def __init__(self, cfg):
        spec = cfg.get("load_allocation")
        validate_allocation_config(spec)
        self.allocate = capacity_weighted
        if spec is not None:
            module, factory = spec["factory"].split(":")
            try:
                self.allocate = getattr(import_module(module), factory)(deepcopy(spec.get("options", {})))
                if not callable(self.allocate):
                    raise TypeError("Factory must return a callable taking AllocationInputs")
            except Exception as exc:
                raise ValueError(f"Cannot load load_allocation {spec['factory']}: {exc}") from exc

    def loads_kw(self, inputs):
        try:
            result = self.allocate(inputs)
            if not isinstance(result, Mapping) or set(result) != {d.id for d in inputs.chillers}:
                raise ValueError("Return exactly one device-id -> load_kw entry for every selected chiller")
            values = {}
            for name, value in result.items():
                if isinstance(value, (str, bytes, bool)):
                    raise ValueError("Loads must be numeric scalars")
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Loads must be finite and nonnegative")
                values[name] = value
            if not math.isclose(sum(values.values()), inputs.load_kw, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("Allocated loads must sum to the requested cooling load")
            for d in inputs.chillers:
                plr = values[d.id] / d.capacity_kw
                if not d.min_plr - 1e-9 <= plr <= d.max_plr + 1e-9:
                    raise AllocationInfeasible(f"{d.id}: allocated PLR outside device bounds")
            return values
        except AllocationInfeasible:
            raise
        except Exception as exc:
            raise ValueError(f"Invalid load allocation: {exc}") from exc
