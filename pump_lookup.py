"""Precompute device subsets; solve common-speed parallel banks analytically."""
from itertools import combinations
from power_models import affinity_power, affinity_head


def subsets(devices):
    return [combo for n in range(1, len(devices) + 1) for combo in combinations(devices, n)]


class PumpLookupTable:
    def __init__(self, devices):
        self.devices = devices
        self.combinations = [(group, sum(d["flow_m3_h"] for d in group), sum(d["power_kw"] for d in group), max(d["min_speed_ratio"] for d in group), min(d["max_speed_ratio"] for d in group)) for group in subsets(devices)]

    def lookup(self, flow_m3_h):
        if flow_m3_h == 0:
            return {"power_kw": 0.0, "flow_m3_h": 0.0, "devices": []}
        best = None
        for devices, flow, power, low, high in self.combinations:
            speed = flow_m3_h / flow
            if low - 1e-9 <= speed <= high + 1e-9:
                candidate = (affinity_power(power, speed), devices, speed)
                if best is None or candidate[0] < best[0]:
                    best = candidate
        if best is None:
            return None
        power, devices, speed = best
        return {"power_kw": power, "flow_m3_h": flow_m3_h, "devices": [{"id": d["id"], "speed_ratio": speed, "frequency_hz": speed * d["rated_hz"], "head_m": affinity_head(d["head_m"], speed)} for d in devices]}
