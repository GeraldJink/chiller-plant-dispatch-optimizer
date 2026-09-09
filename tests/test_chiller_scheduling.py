"""Unit-commitment, runtime preference and replaceable allocation regressions."""
import copy
from datetime import datetime, timedelta
from itertools import product
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import load_config, validate_config
from chiller_scheduling import CommitmentPlanner, replay_commitment, initial_state
from load_allocation import AllocationChiller, AllocationInputs, LoadAllocator, AllocationInfeasible
from examples.load_allocators import create_priority_allocator
from ga_series import SeriesGA
from optimize_utils import validate_schedule


def config(count=2):
    cfg = load_config()
    cfg["chillers"] = cfg["chillers"][:count]
    cfg["optimization"].update(population_size=6, generations=2)
    return cfg


def loads(values, durations=None):
    durations = durations or [1.0] * len(values)
    stamp = datetime(2026, 7, 1)
    result = []
    for value, duration in zip(values, durations):
        result.append({"timestamp": stamp.isoformat(), "load_kw": value, "wet_bulb_c": 24.0, "duration_hours": duration})
        stamp += timedelta(hours=duration)
    return result


def costs(*steps):
    return [{mask: {"total_power_kw": power} for mask, power in step.items()} for step in steps]


def timeline(cfg, rows, masks):
    return [{**row, "chillers": [{"id": d["id"]} for i, d in enumerate(cfg["chillers"]) if mask & (1 << i)]} for row, mask in zip(rows, masks)]


class CommitmentTest(unittest.TestCase):
    def test_dp_looks_ahead_instead_of_greedy_dead_end(self):
        cfg = config()
        rows = loads([500, 500])
        energy, masks = CommitmentPlanner(cfg, rows).solve(costs({1: 1, 2: 2}, {2: 1}))
        self.assertEqual((energy, masks), (3, [2, 2]))
        replay_commitment(cfg, timeline(cfg, rows, masks), annotate=True)

    def test_min_on_rejects_hourly_switching(self):
        cfg = config()
        rows = loads([500] * 4)
        energy, masks = CommitmentPlanner(cfg, rows).solve(costs({1: 1, 2: 10}, {1: 10, 2: 1}, {1: 1, 2: 10}, {1: 10, 2: 1}))
        self.assertEqual(energy, 22)
        replay_commitment(cfg, timeline(cfg, rows, masks), annotate=True)
        with self.assertRaisesRegex(ValueError, "minimum on"):
            replay_commitment(cfg, timeline(cfg, rows, [1, 2, 1, 2]), annotate=True)

    def test_min_off_prevents_restart_after_one_hour(self):
        cfg = config()
        rows = loads([500] * 4)
        energy, masks = CommitmentPlanner(cfg, rows).solve(costs({1: 1}, {1: 1}, {2: 1}, {1: 1}))
        self.assertTrue(math.isinf(energy))
        self.assertIsNone(masks)
        cfg["chiller_scheduling"]["min_on_hours"] = 0
        with self.assertRaisesRegex(ValueError, "minimum off"):
            replay_commitment(cfg, timeline(cfg, rows, [1, 1, 2, 1]), annotate=True)

    def test_elapsed_hours_not_number_of_rows(self):
        cfg = config(1)
        rows = loads([500] * 8, [.25] * 8)
        energy, masks = CommitmentPlanner(cfg, rows).solve(costs(*[{1: 4}] * 8))
        self.assertEqual(energy, 8)
        self.assertEqual(masks, [1] * 8)
        result = timeline(cfg, rows, masks)
        state = replay_commitment(cfg, result, annotate=True)
        self.assertEqual(state["runtime_hours"], [2])
        bad = loads([500] * 7 + [0], [.25] * 8)
        self.assertTrue(math.isinf(CommitmentPlanner(cfg, bad).solve(costs(*([{1: 4}] * 7 + [{0: 0}])))[0]))
        irregular = loads([500, 500, 0], [.3, 1.7, 2])
        energy, masks = CommitmentPlanner(cfg, irregular).solve(costs({1: 10}, {1: 10}, {0: 0}))
        self.assertEqual(energy, 20)
        replay_commitment(cfg, timeline(cfg, irregular, masks), annotate=True)

    def test_initial_on_and_off_locks_and_zero_load_conflict(self):
        cfg = config(1)
        cfg["chiller_scheduling"].update(initial_on=[True], initial_state_hours=[.5], runtime_hours=[100])
        with self.assertRaisesRegex(ValueError, "No feasible schedule"):
            SeriesGA(cfg).optimize(loads([0, 0]))
        rows = loads([500, 0], [1.5, 2])
        _, masks = CommitmentPlanner(cfg, rows).solve(costs({1: 10}, {0: 0}))
        state = replay_commitment(cfg, timeline(cfg, rows, masks), annotate=True)
        self.assertEqual(state["runtime_hours"], [101.5])
        cfg["chiller_scheduling"].update(initial_on=[False], initial_state_hours=[.5])
        self.assertTrue(math.isinf(CommitmentPlanner(cfg, loads([500, 500])).solve(costs({1: 1}, {1: 1}))[0]))
        rows = loads([0, 500], [1.5, 2])
        self.assertEqual(CommitmentPlanner(cfg, rows).solve(costs({0: 0}, {1: 1}))[1], [0, 1])

    def test_terminal_policy_and_continuation_state(self):
        cfg = config(1)
        rows = loads([500])
        self.assertTrue(math.isinf(CommitmentPlanner(cfg, rows).solve(costs({1: 1}))[0]))
        cfg["chiller_scheduling"]["terminal_policy"] = "carry_over"
        result = SeriesGA(cfg).optimize(rows)
        state = result["terminal_chiller_state"]
        self.assertEqual(state["remaining_lock_hours"], [1])
        for key in ("initial_on", "initial_state_hours", "runtime_hours"):
            cfg["chiller_scheduling"][key] = state[key]
        with self.assertRaisesRegex(ValueError, "No feasible schedule"):
            SeriesGA(cfg).optimize(loads([0]))
        next_result = SeriesGA(cfg).optimize(loads([500, 0, 0]))
        self.assertEqual(next_result["terminal_chiller_state"]["runtime_hours"], [2])
        self.assertEqual(next_result["terminal_chiller_state"]["remaining_lock_hours"], [0])

    def test_shorter_runtime_preference_and_energy_priority(self):
        cfg = config()
        cfg["chiller_scheduling"]["runtime_hours"] = [100, 0]
        rows = loads([500] * 4)
        self.assertEqual(CommitmentPlanner(cfg, rows).solve(costs(*[{1: 10, 2: 10}] * 4))[1], [2] * 4)
        self.assertEqual(CommitmentPlanner(cfg, rows).solve(costs(*[{1: 9, 2: 10}] * 4))[1], [1] * 4)
        cfg["chiller_scheduling"]["runtime_hours"] = [0, 0]
        _, masks = CommitmentPlanner(cfg, rows).solve(costs(*[{1: 10, 2: 10}] * 4))
        state = replay_commitment(cfg, timeline(cfg, rows, masks), annotate=True)
        self.assertEqual(state["runtime_hours"], [2, 2])

    def test_full_optimizer_respects_runtime_array_and_validation(self):
        cfg = config()
        cfg["chiller_scheduling"]["runtime_hours"] = [100, 0]
        rows = loads([650, 650])
        result = SeriesGA(cfg).optimize(rows)
        self.assertTrue(all([d["id"] for d in row["chillers"]] == ["CH-2"] for row in result["schedule"]))
        self.assertEqual(result["terminal_chiller_state"]["runtime_hours"], [100, 2])
        bad = copy.deepcopy(result["schedule"])
        bad[0]["chiller_runtime_hours"][1] += 1
        with self.assertRaisesRegex(ValueError, "commitment metadata"):
            validate_schedule(cfg, rows, bad)

    def test_energy_matches_exhaustive_legal_paths(self):
        cfg = config()
        rows = loads([500] * 5)
        options = costs({1: 1, 2: 3, 3: 8}, {1: 4, 2: 1, 3: 7}, {1: 2, 2: 3, 3: 8}, {1: 9, 2: 1, 3: 10}, {1: 2, 2: 5, 3: 8})
        legal_costs = []
        for masks in product((1, 2, 3), repeat=5):
            try:
                replay_commitment(cfg, timeline(cfg, rows, masks), annotate=True)
            except ValueError:
                continue
            legal_costs.append(sum(options[i][mask]["total_power_kw"] for i, mask in enumerate(masks)))
        self.assertEqual(CommitmentPlanner(cfg, rows).solve(options)[0], min(legal_costs))

    def test_configuration_arrays_and_legacy_defaults(self):
        for key, value in (("runtime_hours", [1]), ("runtime_hours", [0, -1]), ("runtime_hours", [0, float("nan")]), ("initial_on", [0, 1]), ("initial_state_hours", [-1, 1]), ("terminal_policy", "ignore"), ("min_on_hours", -1)):
            cfg = config()
            cfg["chiller_scheduling"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_config(cfg)
        cfg = config()
        del cfg["chiller_scheduling"]
        del cfg["load_allocation"]
        validate_config(cfg)
        self.assertEqual(initial_state(cfg), ([False, False], [2, 2], [0, 0]))


class AllocationTest(unittest.TestCase):
    def test_default_capacity_weighting(self):
        group = (AllocationChiller("a", 1000, .15, 1), AllocationChiller("b", 2000, .15, 1))
        inputs = AllocationInputs(1500, group, 8, 13, 29, 34, 24)
        self.assertEqual(LoadAllocator(config()).loads_kw(inputs), {"a": 500, "b": 1000})

    def test_custom_allocator_in_search_and_verification_loaded_once(self):
        cfg = config()
        cfg["load_allocation"] = {"factory": "examples.load_allocators:create_priority_allocator", "options": {"priority_ids": ["CH-2", "CH-1"]}}
        cfg["chiller_scheduling"].update(initial_on=[True, True], initial_state_hours=[0, 0])
        rows = loads([1000, 1000])
        with patch("examples.load_allocators.create_priority_allocator", wraps=create_priority_allocator) as factory:
            result = SeriesGA(cfg).optimize(rows)
            self.assertEqual(factory.call_count, 1)
        for row in result["schedule"]:
            self.assertEqual({d["id"]: d["load_kw"] for d in row["chillers"]}, {"CH-1": 150, "CH-2": 850})
        self.assertTrue(validate_schedule(cfg, rows, result["schedule"]))
        cfg["load_allocation"] = None
        with self.assertRaisesRegex(ValueError, "load allocation model"):
            validate_schedule(cfg, rows, result["schedule"])

    def test_bad_plugin_results_fail_and_out_of_plr_is_infeasible(self):
        cfg = config()
        cfg["load_allocation"] = {"factory": "examples.load_allocators:create_priority_allocator", "options": {}}
        group = (AllocationChiller("a", 1000, .15, 1), AllocationChiller("b", 1000, .15, 1))
        inputs = AllocationInputs(1000, group, 8, 13, 29, 34, 24)
        for value in ({"a": 1000}, {"a": 500, "b": float("nan")}, {"a": -1, "b": 1001}, {"a": 500, "b": 400}, {"a": "500", "b": 500}):
            with self.subTest(value=value), patch("load_allocation.import_module", return_value=SimpleNamespace(create_priority_allocator=lambda options: lambda inputs: value)):
                with self.assertRaisesRegex(ValueError, "Invalid load allocation"):
                    LoadAllocator(cfg).loads_kw(inputs)
        with patch("load_allocation.import_module", return_value=SimpleNamespace(create_priority_allocator=lambda options: lambda inputs: {"a": 900, "b": 100})):
            with self.assertRaises(AllocationInfeasible):
                LoadAllocator(cfg).loads_kw(inputs)


if __name__ == "__main__":
    unittest.main()
