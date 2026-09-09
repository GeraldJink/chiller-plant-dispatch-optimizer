import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from config import load_config, validate_config, CONTROLS, TEMPERATURES
from ga_series import SeriesGA
from load_generate import generate_load, read_load_csv, write_load_csv, validate_load
from optimize_utils import Plant, validate_schedule
from power_models import chiller_cop, affinity_flow, affinity_head, affinity_power, merkel_approach, required_air_fraction
from pump_lookup import PumpLookupTable
from create_pump_tables import create_pump_lookup_tables
from main import main
from build_release import build_release, PUBLIC_FILES
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class ModelsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_cop_peak_is_at_65_percent(self):
        model = self.cfg["chiller_model"]
        samples = [(chiller_cop(i / 100, 8, 34, model), i) for i in range(15, 101)]
        self.assertEqual(max(samples)[1], 65)
        self.assertAlmostEqual(max(samples)[0], 6.5)
        self.assertGreater(chiller_cop(.65, 9, 34, model), chiller_cop(.65, 8, 34, model))
        self.assertLess(chiller_cop(.65, 8, 35, model), chiller_cop(.65, 8, 34, model))

    def test_affinity_laws_and_exact_pump_flow(self):
        self.assertEqual(affinity_flow(100, .5), 50)
        self.assertEqual(affinity_head(20, .5), 5)
        self.assertEqual(affinity_power(16, .5), 2)
        bank = PumpLookupTable(self.cfg["chw_pumps"])
        self.assertIsNone(bank.lookup(1))
        self.assertIsNone(bank.lookup(10000))
        self.assertEqual(bank.lookup(0)["power_kw"], 0)
        result = bank.lookup(480)
        self.assertAlmostEqual(result["power_kw"], 9)
        self.assertEqual(len(result["devices"]), 4)

    def test_tower_full_bank_and_part_load_endpoints(self):
        model = self.cfg["tower_model"]
        plant = Plant(self.cfg, generate_load(self.cfg)[:1])
        for water_range in (3, 5, 6):
            self.assertAlmostEqual(merkel_approach(water_range, 1, model), 3)
            self.assertGreater(merkel_approach(water_range, .5, model), 3)
            for approach in (3, 5, 8):
                air = required_air_fraction(water_range, approach, model)
                self.assertAlmostEqual(merkel_approach(water_range, air, model), approach)
                self.assertIsNotNone(plant.tower_dispatch(2000, water_range, approach))
        full = plant.tower_dispatch(2000, 5, 3)
        self.assertEqual(len(full["devices"]), 4)
        self.assertTrue(all(abs(d["speed_ratio"] - 1) < 1e-9 for d in full["devices"]))
        self.assertAlmostEqual(full["power_kw"], 48)

    def test_config_rejects_bad_values_and_unknown_keys(self):
        for path, value in ((["chillers", 0, "capacity_kw"], -1), (["optimization", "population_size"], 1), (["optimization", "bounds", "approach_c"], [2, 9]), (["optimization", "mutation_rate"], float("nan")), (["tower_model", "typo"], 1), (["optimization", "initial_temperatures", "cw_return_c"], 20)):
            cfg = copy.deepcopy(self.cfg)
            node = cfg
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_config(cfg)


class LoadsTest(unittest.TestCase):
    def test_release_contains_only_public_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            path = build_release(Path(temp) / "source.zip")
            with ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"forecast_and_plan_simple/" + name for name in PUBLIC_FILES})
                self.assertTrue(all(not any(part in name for part in ("__pycache__", ".venv", ".idea", "outputs/", "pump_data")) for name in archive.namelist()))

    def test_generation_and_roundtrip(self):
        cfg = load_config()
        rows = generate_load(cfg)
        self.assertEqual(rows, generate_load(cfg))
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(abs(a["load_kw"] - b["load_kw"]) <= 320.001 for a, b in zip(rows, rows[1:])))
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "load.csv"
            write_load_csv(file, rows)
            self.assertEqual(read_load_csv(file), rows)

    def test_invalid_csv_and_time_values(self):
        rows = generate_load(load_config())[:2]
        for key, value in (("load_kw", -1), ("load_kw", "nan"), ("wet_bulb_c", "inf"), ("duration_hours", 0), ("timestamp", "bad date")):
            bad = copy.deepcopy(rows)
            bad[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_load(bad)
        for stamp in (rows[0]["timestamp"], "2026-07-01T03:00:00", "2026-07-01T01:00:00+00:00"):
            bad = copy.deepcopy(rows)
            bad[1]["timestamp"] = stamp
            with self.assertRaises(ValueError):
                validate_load(bad)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.csv"
            path.write_text("timestamp,load_kw\n2026-01-01,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_load_csv(path)


class OptimizerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["optimization"].update(population_size=8, generations=4)

    def test_horizon_energy_elitism_reproducibility_and_independent_checks(self):
        loads = read_load_csv(ROOT / "examples" / "load.csv")
        result = SeriesGA(self.cfg).optimize(loads)
        self.assertEqual(result, SeriesGA(self.cfg).optimize(loads))
        self.assertTrue(validate_schedule(self.cfg, loads, result["schedule"]))
        self.assertAlmostEqual(result["total_energy_kwh"], sum(r["energy_kwh"] for r in result["schedule"]))
        energies = [h["best_energy_kwh"] for h in result["history"]]
        self.assertTrue(all(b <= a for a, b in zip(energies, energies[1:])))
        self.assertLessEqual(result["total_energy_kwh"], result["initial_population_best_energy_kwh"])
        schedule = copy.deepcopy(result["schedule"])
        schedule[6]["chw_return_c"] += 2
        with self.assertRaises(ValueError):
            validate_schedule(self.cfg, loads, schedule)
        schedule = copy.deepcopy(result["schedule"])
        schedule[6]["cw_flow_m3_h"] *= 2
        with self.assertRaises(ValueError):
            validate_schedule(self.cfg, loads, schedule)

    def test_zero_load_retains_setpoint_continuity(self):
        loads = generate_load(self.cfg)[:3]
        for row in loads:
            row["load_kw"] = 0
        result = SeriesGA(self.cfg).optimize(loads)
        self.assertEqual(result["total_energy_kwh"], 0)
        for row in result["schedule"]:
            self.assertEqual(row["chillers"], [])
            self.assertGreater(row["chw_supply_c"], 0)
        self.assertTrue(validate_schedule(self.cfg, loads, result["schedule"]))

    def test_non_hourly_energy(self):
        self.cfg["load_generation"].update(periods=8, interval_minutes=15)
        loads = generate_load(self.cfg)
        result = SeriesGA(self.cfg).optimize(loads)
        self.assertAlmostEqual(result["total_energy_kwh"], .25 * sum(r["total_power_kw"] for r in result["schedule"]))

    def test_custom_equipment_models_bounds_and_initial_state(self):
        cfg = self.cfg
        cfg["chillers"] = cfg["chillers"][:2]
        cfg["chillers"][0].update(capacity_kw=800, model={"peak_cop": 7, "peak_plr": .62})
        cfg["chillers"][1].update(capacity_kw=1200, cop_multiplier=.9)
        for group in ("chw_pumps", "cw_pumps", "towers"):
            cfg[group] = cfg[group][:2]
        cfg["optimization"]["bounds"]["chw_supply_c"] = [8, 10]
        cfg["optimization"]["max_temperature_step_c"] = .5
        cfg["optimization"]["initial_temperatures"] = None
        cfg["load_generation"]["periods"] = 5
        loads = generate_load(cfg)
        result = SeriesGA(cfg).optimize(loads)
        self.assertTrue(validate_schedule(cfg, loads, result["schedule"]))

    def test_infeasibility_is_reported(self):
        loads = generate_load(self.cfg)[:2]
        loads[0]["load_kw"] = 10000
        with self.assertRaisesRegex(ValueError, "exceeds"):
            SeriesGA(self.cfg).optimize(loads)
        loads[0]["load_kw"] = 1
        with self.assertRaisesRegex(ValueError, "No feasible schedule"):
            SeriesGA(self.cfg).optimize(loads)
        loads = generate_load(self.cfg)[:2]
        loads[0]["wet_bulb_c"], loads[1]["wet_bulb_c"] = 10, 30
        with self.assertRaisesRegex(ValueError, "Infeasible"):
            SeriesGA(self.cfg).optimize(loads)

    def test_cli_outputs_are_machine_readable_and_pump_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.cfg["load_generation"]["periods"] = 3
            config_path = root / "custom.json"
            config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
            self.assertEqual(main(["--config", str(config_path), "--generate-only", str(root / "input.csv")]), 0)
            args = ["--config", str(config_path), "--load", str(root / "input.csv"), "--output", str(root / "result"), "--quiet"]
            self.assertEqual(main(args), 0)
            result = json.loads((root / "result" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "feasible_best_found")
            self.assertTrue((root / "result" / "schedule.csv").is_file())
            self.assertTrue((root / "result" / "convergence.csv").is_file())
            create_pump_lookup_tables(self.cfg, root / "tables", points=5)
            self.assertTrue((root / "tables" / "chw_pumps.csv").is_file())


if __name__ == "__main__":
    unittest.main()
