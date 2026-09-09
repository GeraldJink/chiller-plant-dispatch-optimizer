"""Feasible temperature decoding, load balance and independent result checks."""
import math
from config import CONTROLS, TEMPERATURES
from power_models import required_air_fraction, merkel_approach, affinity_power
from model_interface import ChillerInputs, ModelDomainError, load_chiller_models
from pump_lookup import PumpLookupTable, subsets
from load_allocation import AllocationChiller, AllocationInputs, AllocationInfeasible, LoadAllocator
from chiller_scheduling import CommitmentPlanner, replay_commitment


def temperatures(controls):
    supply, delta, cool_delta, cool_supply = controls
    return {"chw_supply_c": supply, "chw_return_c": supply + delta, "cw_supply_c": cool_supply, "cw_return_c": cool_supply + cool_delta}


def controls_from_temperatures(values):
    return [values["chw_supply_c"], values["chw_return_c"] - values["chw_supply_c"], values["cw_return_c"] - values["cw_supply_c"], values["cw_supply_c"]]


class Plant:
    def __init__(self, cfg, loads):
        self.cfg, self.loads = cfg, loads
        self.bounds = cfg["optimization"]["bounds"]
        self.chiller_models = load_chiller_models(cfg)
        self.allocator = LoadAllocator(cfg)
        self.commitment = CommitmentPlanner(cfg, loads)
        self.chiller_indices = {d["id"]: i for i, d in enumerate(cfg["chillers"])}
        self.allocation_chillers = {d["id"]: AllocationChiller(d["id"], d["capacity_kw"], d["min_plr"], d["max_plr"]) for d in cfg["chillers"]}
        self.chw_pumps = PumpLookupTable(cfg["chw_pumps"])
        self.cw_pumps = PumpLookupTable(cfg["cw_pumps"])
        self.chiller_groups = [(g, sum(d["capacity_kw"] for d in g)) for g in subsets(cfg["chillers"])]
        self.tower_groups = [(g, sum(d["heat_rejection_kw"] for d in g), sum(d["fan_power_kw"] for d in g), max(d["min_speed_ratio"] for d in g), min(d["max_speed_ratio"] for d in g)) for g in subsets(cfg["towers"])]
        self.tower_capacity = sum(d["heat_rejection_kw"] for d in cfg["towers"])
        capacity = sum(d["capacity_kw"] * d["max_plr"] for d in cfg["chillers"])
        for row in loads:
            if row["load_kw"] > capacity + 1e-8:
                raise ValueError(f"{row['timestamp']}: load exceeds configured chiller capacity ({capacity:g} kW)")
        # Future-aware supply intervals stop an early setpoint stranding later wet-bulb steps.
        self.supply_intervals = {}
        step = min(cfg["optimization"]["max_temperature_step_c"], cfg["optimization"]["max_control_step_c"])
        for key in ("chw_supply_c", "cw_supply_c"):
            intervals = []
            for row in loads:
                low, high = self.bounds[key]
                if key == "cw_supply_c":
                    low = max(low, row["wet_bulb_c"] + self.bounds["approach_c"][0])
                    high = min(high, row["wet_bulb_c"] + self.bounds["approach_c"][1])
                intervals.append([low, high])
            for i in range(len(intervals) - 2, -1, -1):
                intervals[i][0] = max(intervals[i][0], intervals[i + 1][0] - step)
                intervals[i][1] = min(intervals[i][1], intervals[i + 1][1] + step)
            if any(low > high + 1e-9 for low, high in intervals):
                raise ValueError(f"Infeasible {key} bounds / wet-bulb sequence / temperature step limit")
            self.supply_intervals[key] = intervals

    def decode(self, genes):
        """Map [0,1] genes into hard bounds, both return temperatures and slew limits."""
        opt = self.cfg["optimization"]
        prev = opt["initial_temperatures"]
        controls = []
        for i in range(len(self.loads)):
            current = [0.0] * 4
            prev_controls = controls_from_temperatures(prev) if prev else None
            for prefix, supply_index, delta_index in (("chw", 0, 1), ("cw", 3, 2)):
                supply_key, return_key, delta_key = f"{prefix}_supply_c", f"{prefix}_return_c", f"{prefix}_delta_c"
                low, high = self.supply_intervals[supply_key][i]
                dlow, dhigh = self.bounds[delta_key]
                rlow, rhigh = self.bounds[return_key]
                if prev:
                    ts, cs = opt["max_temperature_step_c"], opt["max_control_step_c"]
                    low, high = max(low, prev[supply_key] - min(ts, cs)), min(high, prev[supply_key] + min(ts, cs))
                    rlow, rhigh = max(rlow, prev[return_key] - ts), min(rhigh, prev[return_key] + ts)
                    dlow, dhigh = max(dlow, prev_controls[delta_index] - cs), min(dhigh, prev_controls[delta_index] + cs)
                low, high = max(low, rlow - dhigh), min(high, rhigh - dlow)
                if low > high + 1e-9 or dlow > dhigh + 1e-9:
                    return None
                supply = low + genes[4 * i + supply_index] * max(0, high - low)
                dlow, dhigh = max(dlow, rlow - supply), min(dhigh, rhigh - supply)
                if dlow > dhigh + 1e-9:
                    return None
                current[supply_index] = supply
                current[delta_index] = dlow + genes[4 * i + delta_index] * max(0, dhigh - dlow)
            controls.append(current)
            prev = temperatures(current)
        return controls

    def tower_dispatch(self, heat_kw, range_c, approach_c):
        model = self.cfg["tower_model"]
        required_air = required_air_fraction(range_c, approach_c, model) * self.tower_capacity
        best = None
        for devices, capacity, power, low, high in self.tower_groups:
            speed = required_air / capacity
            if heat_kw <= capacity + 1e-8 and low - 1e-9 <= speed <= high + 1e-9:
                value = affinity_power(power, speed)
                if best is None or value < best[0]:
                    best = (value, devices, speed)
        if best is None:
            return None
        power, devices, speed = best
        return {"power_kw": power, "devices": [{"id": d["id"], "speed_ratio": speed, "frequency_hz": speed * d["rated_hz"]} for d in devices]}

    def dispatch_candidates(self, load, wet_bulb, controls):
        t = temperatures(controls)
        approach = t["cw_supply_c"] - wet_bulb
        common = {**dict(zip(CONTROLS, controls)), **t, "approach_c": approach}
        if load == 0:
            return {0: {**common, "chillers": [], "chw_pumps": [], "cw_pumps": [], "towers": [], "chw_flow_m3_h": 0.0, "cw_flow_m3_h": 0.0, "heat_rejection_kw": 0.0, "chiller_power_kw": 0.0, "chw_pump_power_kw": 0.0, "cw_pump_power_kw": 0.0, "tower_power_kw": 0.0, "total_power_kw": 0.0}}
        cp = self.cfg["water_heat_capacity_kwh_m3_k"]
        chw = self.chw_pumps.lookup(load / (cp * controls[1]))
        if chw is None:
            return {}
        candidates = {}
        for devices, capacity in self.chiller_groups:
            if load < sum(d["capacity_kw"] * d["min_plr"] for d in devices) - 1e-9 or load > sum(d["capacity_kw"] * d["max_plr"] for d in devices) + 1e-9:
                continue
            allocation_inputs = AllocationInputs(load, tuple(self.allocation_chillers[d["id"]] for d in devices), **t, wet_bulb_c=wet_bulb)
            try:
                allocated = self.allocator.loads_kw(allocation_inputs)
            except AllocationInfeasible:
                continue
            chillers = []
            for d in devices:
                q = allocated[d["id"]]
                plr = q / d["capacity_kw"]
                inputs = ChillerInputs(q, d["capacity_kw"], **t, wet_bulb_c=wet_bulb)
                try:
                    power = self.chiller_models[d["id"]].power_kw(inputs)
                except ModelDomainError:
                    break  # Reject only this subset; other chillers may cover this point.
                chillers.append({"id": d["id"], "load_kw": q, "plr": plr, "cop": q / power, "power_kw": power})
            if len(chillers) != len(devices):
                continue
            power = sum(d["power_kw"] for d in chillers)
            reject = load + power
            cw = self.cw_pumps.lookup(reject / (cp * controls[2]))
            tower = self.tower_dispatch(reject, controls[2], approach)
            if cw is None or tower is None:
                continue
            total = power + chw["power_kw"] + cw["power_kw"] + tower["power_kw"]
            mask = sum(1 << self.chiller_indices[d["id"]] for d in devices)
            candidates[mask] = {**common, "chillers": chillers, "chw_pumps": chw["devices"], "cw_pumps": cw["devices"], "towers": tower["devices"], "chw_flow_m3_h": chw["flow_m3_h"], "cw_flow_m3_h": cw["flow_m3_h"], "heat_rejection_kw": reject, "chiller_power_kw": power, "chw_pump_power_kw": chw["power_kw"], "cw_pump_power_kw": cw["power_kw"], "tower_power_kw": tower["power_kw"], "total_power_kw": total}
        return candidates

    def dispatch(self, load, wet_bulb, controls):
        """Single-point diagnostic only; full planning must use evaluate()."""
        return min(self.dispatch_candidates(load, wet_bulb, controls).values(), key=lambda row: row["total_power_kw"], default=None)

    def evaluate(self, genes, detailed=False):
        controls = self.decode(genes)
        if controls is None:
            return (math.inf, None)
        candidates = []
        for source, values in zip(self.loads, controls):
            options = self.dispatch_candidates(source["load_kw"], source["wet_bulb_c"], values)
            if not options:
                return (math.inf, None)
            candidates.append(options)
        energy, masks = self.commitment.solve(candidates)
        if masks is None or not detailed:
            return energy, None
        schedule = [{**source, **options[mask], "energy_kwh": options[mask]["total_power_kw"] * source["duration_hours"]} for source, options, mask in zip(self.loads, candidates, masks)]
        replay_commitment(self.cfg, schedule, annotate=True)
        return energy, schedule


def validate_schedule(cfg, loads, schedule, chiller_models=None, allocator=None):
    """Check exported decisions independently of chromosome repair and GA fitness."""
    def check(ok, message):
        if not ok:
            raise ValueError(f"Schedule validation failed: {message}")

    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-6)

    check(len(loads) == len(schedule), "row count")
    replay_commitment(cfg, schedule)
    chiller_models = load_chiller_models(cfg) if chiller_models is None else chiller_models
    allocator = LoadAllocator(cfg) if allocator is None else allocator
    opt, cp = cfg["optimization"], cfg["water_heat_capacity_kwh_m3_k"]
    previous = opt["initial_temperatures"]
    for source, row in zip(loads, schedule):
        check(all(row[key] == source[key] for key in source), "source row changed")
        values = [row[key] for key in CONTROLS]
        expected = temperatures(values)
        check(all(close(row[key], expected[key]) for key in TEMPERATURES), "supply/return balance")
        check(close(row["approach_c"], row["cw_supply_c"] - source["wet_bulb_c"]), "approach")
        for key, (low, high) in opt["bounds"].items():
            check(math.isfinite(row[key]) and low - 1e-7 <= row[key] <= high + 1e-7, f"{key} bounds")
        if previous:
            check(all(abs(row[key] - previous[key]) <= opt["max_temperature_step_c"] + 1e-7 for key in TEMPERATURES), "four water temperature steps")
            check(all(abs(a - b) <= opt["max_control_step_c"] + 1e-7 for a, b in zip(values, controls_from_temperatures(previous))), "four control steps")
        previous = expected
        load = source["load_kw"]
        check(close(sum(d["load_kw"] for d in row["chillers"]), load), "chiller load balance")
        for group in ("chillers", "chw_pumps", "cw_pumps", "towers"):
            ids = [d["id"] for d in row[group]]
            check(len(ids) == len(set(ids)), f"duplicate {group}")
        devices = {d["id"]: d for d in cfg["chillers"]}
        if load:
            selected = {d["id"] for d in row["chillers"]}
            group = tuple(AllocationChiller(d["id"], d["capacity_kw"], d["min_plr"], d["max_plr"]) for d in cfg["chillers"] if d["id"] in selected)
            allocation = allocator.loads_kw(AllocationInputs(load, group, **expected, wet_bulb_c=source["wet_bulb_c"]))
            check(all(close(d["load_kw"], allocation[d["id"]]) for d in row["chillers"]), "load allocation model")
        for d in row["chillers"]:
            check(d["id"] in devices, "unknown chiller")
            rated = devices[d["id"]]
            check(rated["min_plr"] - 1e-7 <= d["plr"] <= rated["max_plr"] + 1e-7, "chiller PLR")
            check(close(d["load_kw"], rated["capacity_kw"] * d["plr"]), "chiller capacity")
            inputs = ChillerInputs(d["load_kw"], rated["capacity_kw"], **expected, wet_bulb_c=source["wet_bulb_c"])
            power = chiller_models[d["id"]].power_kw(inputs)
            check(close(d["cop"], d["load_kw"] / power) and close(d["power_kw"], power), "chiller model")
        check(close(row["chiller_power_kw"], sum(d["power_kw"] for d in row["chillers"])), "chiller power sum")
        for group, power_key, flow_key in (("chw_pumps", "chw_pump_power_kw", "chw_flow_m3_h"), ("cw_pumps", "cw_pump_power_kw", "cw_flow_m3_h"), ("towers", "tower_power_kw", None)):
            devices = {d["id"]: d for d in cfg[group]}
            flow, power = 0.0, 0.0
            for d in row[group]:
                check(d["id"] in devices, f"unknown {group}")
                rated, speed = devices[d["id"]], d["speed_ratio"]
                check(rated["min_speed_ratio"] - 1e-7 <= speed <= rated["max_speed_ratio"] + 1e-7, f"{group} speed")
                check(close(d["frequency_hz"], speed * rated["rated_hz"]), "frequency")
                power += rated["fan_power_kw" if group == "towers" else "power_kw"] * speed ** 3
                if flow_key:
                    flow += rated["flow_m3_h"] * speed
                    check(close(d["head_m"], rated["head_m"] * speed ** 2), "pump head")
            check(close(power, row[power_key]), f"{group} power")
            if flow_key:
                check(close(flow, row[flow_key]), f"{group} flow")
        check(close(row["chw_flow_m3_h"] * cp * row["chw_delta_c"], load), "CHW heat balance")
        reject = load + row["chiller_power_kw"]
        check(close(row["heat_rejection_kw"], reject), "heat rejection")
        check(close(row["cw_flow_m3_h"] * cp * row["cw_delta_c"], reject), "CW heat balance")
        if load:
            towers = {d["id"]: d for d in cfg["towers"]}
            active_capacity = sum(towers[d["id"]]["heat_rejection_kw"] for d in row["towers"])
            check(reject <= active_capacity + 1e-6, "tower heat capacity")
            air = sum(towers[d["id"]]["heat_rejection_kw"] * d["speed_ratio"] for d in row["towers"]) / sum(d["heat_rejection_kw"] for d in cfg["towers"])
            check(close(merkel_approach(row["cw_delta_c"], air, cfg["tower_model"]), row["approach_c"]), "Merkel tower approach")
        else:
            check(all(not row[g] for g in ("chillers", "chw_pumps", "cw_pumps", "towers")), "zero load must stop all equipment")
        total = sum(row[k] for k in ("chiller_power_kw", "chw_pump_power_kw", "cw_pump_power_kw", "tower_power_kw"))
        check(math.isfinite(total) and total >= 0 and close(total, row["total_power_kw"]), "total power")
        check(close(total * source["duration_hours"], row["energy_kwh"]), "energy accounting")
    return True
