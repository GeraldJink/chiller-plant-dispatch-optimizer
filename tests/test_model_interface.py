"""Custom model contract tests; optional framework tests run when already installed."""
import copy
from importlib.util import find_spec
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import load_config, validate_config
from examples.model_adapters import create_mechanistic, feature_vector
from examples.use_custom_model import demo_config
from ga_series import SeriesGA
from load_generate import generate_load
from model_interface import ChillerInputs, ChillerPowerModel, ModelDomainError
from optimize_utils import validate_schedule


def example_inputs():
    return ChillerInputs(650, 1000, 8, 13, 29, 34, 24)


def small_config():
    cfg = demo_config()
    cfg["load_generation"]["periods"] = 3
    cfg["optimization"].update(population_size=6, generations=2)
    return cfg


class ModelInterfaceTest(unittest.TestCase):
    def test_mechanistic_formula_and_feature_order(self):
        x = example_inputs()
        expected_cop = .6 * (8 - 2 + 273.15) / ((34 + 3) - (8 - 2))
        self.assertAlmostEqual(create_mechanistic({})(x), 650 / expected_cop)
        self.assertEqual(feature_vector(x), [650, 8, 34])

    def test_custom_model_is_used_in_search_and_validation_loaded_once(self):
        cfg = small_config()
        loads = generate_load(cfg)
        with patch("examples.model_adapters.create_mechanistic", wraps=create_mechanistic) as factory:
            result = SeriesGA(cfg).optimize(loads)
            self.assertEqual(factory.call_count, 4)
        self.assertTrue(validate_schedule(cfg, loads, result["schedule"]))
        builtin = copy.deepcopy(cfg)
        for device in builtin["chillers"]:
            del device["predictor"]
        self.assertNotAlmostEqual(result["total_energy_kwh"], SeriesGA(builtin).optimize(loads)["total_energy_kwh"])
        with self.assertRaisesRegex(ValueError, "chiller model"):
            validate_schedule(builtin, loads, result["schedule"])

    def test_custom_predictor_does_not_apply_builtin_cop_multiplier(self):
        cfg = demo_config()
        device = cfg["chillers"][0]
        original = ChillerPowerModel(cfg, device).power_kw(example_inputs())
        device.update(cop_multiplier=2, model={"peak_cop": 12})
        self.assertEqual(original, ChillerPowerModel(cfg, device).power_kw(example_inputs()))

    def test_domain_rejection_keeps_other_device_combinations(self):
        cfg = small_config()
        cfg["chillers"][0]["predictor"]["domain"]["plr"] = [.99, 1]
        loads = generate_load(cfg)
        result = SeriesGA(cfg).optimize(loads)
        self.assertTrue(all(d["id"] != "CH-1" for row in result["schedule"] for d in row["chillers"]))
        for device in cfg["chillers"]:
            device["predictor"]["domain"]["chw_supply_c"] = [50, 60]
        with self.assertRaisesRegex(ValueError, "No feasible schedule"):
            SeriesGA(cfg).optimize(loads)

    def test_invalid_predictions_fail_instead_of_becoming_fitness(self):
        cfg = demo_config()
        for value in (0, -1, float("nan"), float("inf"), "100", True, [100]):
            factory = SimpleNamespace(create_mechanistic=lambda options: lambda inputs: value)
            with self.subTest(value=value), patch("model_interface.import_module", return_value=factory):
                model = ChillerPowerModel(cfg, cfg["chillers"][0])
                with self.assertRaisesRegex(ValueError, "CH-1: invalid model prediction"):
                    model.power_kw(example_inputs())

    def test_config_and_missing_factory_fail_clearly(self):
        for key, value in (("factory", "bad:path:format"), ("domain", {}), ("options", []), ("typo", 1)):
            cfg = demo_config()
            cfg["chillers"][0]["predictor"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(cfg)
        cfg = demo_config()
        cfg["chillers"][0]["predictor"]["factory"] = "examples.model_adapters:missing_factory"
        with self.assertRaisesRegex(ValueError, "cannot load predictor"):
            ChillerPowerModel(cfg, cfg["chillers"][0])

    def test_zero_load_never_invokes_custom_prediction(self):
        cfg = small_config()
        loads = generate_load(cfg)
        for row in loads:
            row["load_kw"] = 0
        def never(inputs):
            raise RuntimeError("Zero load must not call inference")
        with patch("model_interface.import_module", return_value=SimpleNamespace(create_mechanistic=lambda options: never)):
            self.assertEqual(SeriesGA(cfg).optimize(loads)["total_energy_kwh"], 0)

    @unittest.skipUnless(all(find_spec(name) is not None for name in ("numpy", "sklearn", "joblib")), "optional sklearn/joblib not installed")
    def test_sklearn_pipeline_artifact_roundtrip_and_plan(self):
        import numpy as np
        import joblib
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LinearRegression
        X = np.array([[q, ts, tr] for q in (300, 600, 900) for ts in (6, 8, 10) for tr in (28, 34, 40)])
        y = X[:, 0] / 6 + 0.3 * (X[:, 2] - X[:, 1])
        pipeline = make_pipeline(StandardScaler(), LinearRegression()).fit(X, y)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "pipeline.joblib"
            joblib.dump(pipeline, file)
            cfg = small_config()
            for device in cfg["chillers"]:
                device["predictor"].update(factory="examples.model_adapters:create_sklearn", options={"artifact": str(file)})
            model = ChillerPowerModel(cfg, cfg["chillers"][0])
            self.assertAlmostEqual(model.power_kw(example_inputs()), float(pipeline.predict([[650, 8, 34]])[0]))
            result = SeriesGA(cfg).optimize(generate_load(cfg))
            self.assertGreater(result["total_energy_kwh"], 0)

    @unittest.skipUnless(find_spec("torch") is not None, "optional torch not installed")
    def test_torch_weights_normalization_and_plan(self):
        import torch
        net = torch.nn.Sequential(torch.nn.Linear(3, 16), torch.nn.ReLU(), torch.nn.Linear(16, 1))
        with torch.no_grad():
            for p in net.parameters():
                p.fill_(0.1)
            net[-1].bias.fill_(100)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "net.pt"
            torch.save(net.state_dict(), file)
            cfg = small_config()
            options = {"artifact": str(file), "mean": [500, 8, 34], "scale": [200, 2, 4]}
            for device in cfg["chillers"]:
                device["predictor"].update(factory="examples.model_adapters:create_torch", options=options)
            model = ChillerPowerModel(cfg, cfg["chillers"][0])
            with torch.no_grad():
                expected = float(net(torch.tensor([[.75, 0, 0]])).item())
            self.assertEqual(model.power_kw(example_inputs()), expected)
            self.assertEqual(model.power_kw(example_inputs()), expected)
            result = SeriesGA(cfg).optimize(generate_load(cfg))
            self.assertGreater(result["total_energy_kwh"], 0)


if __name__ == "__main__":
    unittest.main()
