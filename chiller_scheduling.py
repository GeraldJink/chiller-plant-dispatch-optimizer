"""Whole-horizon unit commitment with real-duration minimum up/down times."""
import math

EPS = 1e-9
DEFAULTS = {
    "min_on_hours": 2.0,
    "min_off_hours": 2.0,
    "initial_on": None,
    "initial_state_hours": None,
    "runtime_hours": None,
    "prefer_shorter_runtime": True,
    "terminal_policy": "require_complete",
}


def scheduling_config(cfg):
    return {**DEFAULTS, **cfg.get("chiller_scheduling", {})}


def initial_state(cfg):
    opts = scheduling_config(cfg)
    count = len(cfg["chillers"])
    on = list(opts["initial_on"]) if opts["initial_on"] is not None else [False] * count
    ages = list(opts["initial_state_hours"]) if opts["initial_state_hours"] is not None else [opts["min_on_hours"] if value else opts["min_off_hours"] for value in on]
    runtimes = list(opts["runtime_hours"]) if opts["runtime_hours"] is not None else [age if value else 0.0 for value, age in zip(on, ages)]
    return on, ages, runtimes


def validate_scheduling_config(cfg):
    spec = cfg.get("chiller_scheduling", {})
    if not isinstance(spec, dict) or set(spec) - set(DEFAULTS):
        raise ValueError("chiller_scheduling: unknown fields or invalid object")
    opts = scheduling_config(cfg)
    for name in ("min_on_hours", "min_off_hours"):
        value = opts[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"chiller_scheduling.{name}: finite hours >= 0 required")
    count = len(cfg["chillers"])
    for name in ("initial_on", "initial_state_hours", "runtime_hours"):
        values = opts[name]
        if values is None:
            continue
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(f"chiller_scheduling.{name}: array must match chillers order and count ({count})")
        for value in values:
            if name == "initial_on":
                valid = type(value) is bool
            else:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            if not valid:
                raise ValueError(f"chiller_scheduling.{name}: invalid value")
    if type(opts["prefer_shorter_runtime"]) is not bool:
        raise ValueError("prefer_shorter_runtime must be boolean")
    if opts["terminal_policy"] not in ("require_complete", "carry_over"):
        raise ValueError("terminal_policy must be require_complete or carry_over")
    on, ages, runtimes = initial_state(cfg)
    if any(value and runtime + EPS < age for value, age, runtime in zip(on, ages, runtimes)):
        raise ValueError("Running chiller runtime_hours cannot be less than initial_state_hours")


class CommitmentPlanner:
    def __init__(self, cfg, loads):
        self.cfg, self.loads = cfg, loads
        self.opts = scheduling_config(cfg)
        self.count = len(cfg["chillers"])
        on, ages, self.runtimes = initial_state(cfg)
        mask = sum(1 << i for i, active in enumerate(on) if active)
        locks = tuple(round(max(0.0, (self.opts["min_on_hours"] if active else self.opts["min_off_hours"]) - age), 10) for active, age in zip(on, ages))
        self.initial = (mask, locks)
        self.layers = None

    def _transition(self, state, mask, duration):
        previous, locks = state
        changed = previous ^ mask
        if any(changed & (1 << i) and lock > EPS for i, lock in enumerate(locks)):
            return None
        next_locks = []
        for i, lock in enumerate(locks):
            if changed & (1 << i):
                lock = self.opts["min_on_hours"] if mask & (1 << i) else self.opts["min_off_hours"]
            next_locks.append(round(max(0.0, lock - duration), 10))
        return mask, tuple(next_locks)

    def _build_layers(self):
        states = [self.initial]
        self.layers = []
        devices = self.cfg["chillers"]
        for row in self.loads:
            if row["load_kw"] == 0:
                masks = [0]
            else:
                masks = [mask for mask in range(1, 1 << self.count)
                         if sum(d["capacity_kw"] * d["min_plr"] for i, d in enumerate(devices) if mask & (1 << i)) <= row["load_kw"] + EPS
                         and sum(d["capacity_kw"] * d["max_plr"] for i, d in enumerate(devices) if mask & (1 << i)) >= row["load_kw"] - EPS]
            indices, next_states, incoming = {}, [], []
            for previous_index, state in enumerate(states):
                for mask in masks:
                    next_state = self._transition(state, mask, row["duration_hours"])
                    if next_state is None:
                        continue
                    if next_state not in indices:
                        indices[next_state] = len(next_states)
                        next_states.append(next_state)
                        incoming.append([])
                    incoming[indices[next_state]].append(previous_index)
            self.layers.append((next_states, incoming))
            states = next_states

    def solve(self, candidates):
        """Exact energy DP for fixed controls/allocator; runtime is a tie-break heuristic.

        One label is kept per on/off and lock state. Runtime history does not
        change feasibility or power, so energy dominance remains valid.
        """
        if self.layers is None:
            self._build_layers()
        # Label: energy, incremental squared-runtime score, added hours, path node.
        labels = [(0.0, 0.0, (0.0,) * self.count, None)]
        prefer = self.opts["prefer_shorter_runtime"]
        for row, options, (states, incoming) in zip(self.loads, candidates, self.layers):
            duration = row["duration_hours"]
            next_labels = [None] * len(states)
            for index, ((mask, _), predecessors) in enumerate(zip(states, incoming)):
                if mask not in options:
                    continue
                cost = options[mask]["total_power_kw"] * duration
                best = None
                for predecessor in predecessors:
                    old = labels[predecessor]
                    if old is None:
                        continue
                    energy = old[0] + cost
                    if best is not None and energy > best[0] + EPS:
                        continue
                    hours = tuple(value + (duration if mask & (1 << i) else 0.0) for i, value in enumerate(old[2]))
                    score = sum(2 * initial * added + added ** 2 for initial, added in zip(self.runtimes, hours)) if prefer else 0.0
                    if best is None or energy < best[0] - EPS or (abs(energy - best[0]) <= EPS and score < best[1]):
                        best = (energy, score, hours, (old[3], mask))
                next_labels[index] = best
            labels = next_labels
        best = None
        final_states = self.layers[-1][0]
        for state, label in zip(final_states, labels):
            if label is None or (self.opts["terminal_policy"] == "require_complete" and any(lock > EPS for lock in state[1])):
                continue
            if best is None or label[0] < best[0] - EPS or (abs(label[0] - best[0]) <= EPS and label[1] < best[1]):
                best = label
        if best is None:
            return math.inf, None
        masks, node = [], best[3]
        while node is not None:
            node, mask = node
            masks.append(mask)
        masks.reverse()
        return best[0], masks


def replay_commitment(cfg, schedule, annotate=False):
    """Independent elapsed-hour validation; no reliance on the DP lock graph."""
    opts = scheduling_config(cfg)
    on, ages, runtimes = initial_state(cfg)
    ids = [d["id"] for d in cfg["chillers"]]
    remaining = []
    for row in schedule:
        selected = {d["id"] for d in row["chillers"]}
        if selected - set(ids):
            raise ValueError("Unknown chiller in commitment schedule")
        next_on = [name in selected for name in ids]
        for i, active in enumerate(next_on):
            minimum = opts["min_on_hours"] if on[i] else opts["min_off_hours"]
            if active != on[i]:
                if ages[i] + 1e-7 < minimum:
                    raise ValueError(f"{row['timestamp']}: {ids[i]} violates minimum {'on' if on[i] else 'off'} time ({minimum:g} h)")
                ages[i] = 0.0
            ages[i] += row["duration_hours"]
            if active:
                runtimes[i] += row["duration_hours"]
        on = next_on
        remaining = [max(0.0, (opts["min_on_hours"] if active else opts["min_off_hours"]) - age) for active, age in zip(on, ages)]
        metadata = {"chiller_on": list(on), "chiller_runtime_hours": list(runtimes), "chiller_state_hours": list(ages), "chiller_remaining_lock_hours": list(remaining)}
        if annotate:
            row.update(metadata)
        else:
            for name, values in metadata.items():
                actual = row.get(name)
                valid_type = (lambda value: type(value) is bool) if name == "chiller_on" else (lambda value: type(value) in (int, float))
                if not isinstance(actual, list) or len(actual) != len(values) or not all(valid_type(value) for value in actual):
                    raise ValueError(f"Invalid commitment metadata: {name}")
                if any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-7) for a, b in zip(actual, values)):
                    raise ValueError(f"Incorrect commitment metadata: {name}")
    if opts["terminal_policy"] == "require_complete" and any(value > 1e-7 for value in remaining):
        raise ValueError("Horizon ends before a minimum on/off duration completes")
    return {"chiller_ids": ids, "initial_on": on, "initial_state_hours": ages, "runtime_hours": runtimes, "remaining_lock_hours": remaining}
