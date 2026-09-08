"""Model factories. No real equipment data or trained model artifacts are included."""
import math
from model_interface import ModelDomainError

# The training columns must use exactly this order and these physical units.
FEATURE_NAMES = ("load_kw", "chw_supply_c", "cw_return_c")


def feature_vector(inputs):
    return [getattr(inputs, name) for name in FEATURE_NAMES]


def create_mechanistic(options):
    """Fictional Carnot-based COP with an explicit part-load correction."""
    allowed = {"efficiency", "evaporator_approach_c", "condenser_approach_c"}
    if set(options) - allowed:
        raise ValueError("Unknown mechanistic option")
    efficiency = float(options.get("efficiency", 0.6))
    evap = float(options.get("evaporator_approach_c", 2.0))
    cond = float(options.get("condenser_approach_c", 3.0))
    if not all(math.isfinite(v) for v in (efficiency, evap, cond)) or not 0 < efficiency <= 1 or min(evap, cond) < 0:
        raise ValueError("Require 0 < efficiency <= 1 and nonnegative approaches")

    def predict(inputs):
        evaporating_k = inputs.chw_supply_c - evap + 273.15
        condensing_k = inputs.cw_return_c + cond + 273.15
        if not 0 < evaporating_k < condensing_k:
            raise ModelDomainError("Carnot temperatures require 0 < Te < Tc")
        correction = 1 / (1 + 2.5 * (inputs.plr - 0.65) ** 2)
        cop = efficiency * evaporating_k / (condensing_k - evaporating_k) * correction
        return inputs.load_kw / cop

    return predict


def create_sklearn(options):
    """Load a trusted, fitted single-output Pipeline predicting unscaled power kW."""
    import joblib
    import numpy as np

    if set(options) != {"artifact"}:
        raise ValueError("sklearn options must contain only artifact")
    pipeline = joblib.load(options["artifact"])

    def predict(inputs):
        features = np.asarray([feature_vector(inputs)], dtype=float)
        output = np.asarray(pipeline.predict(features))
        if output.shape != (1,):
            raise ValueError("Expected single-output regressor prediction shape (1,)")
        return float(output[0])

    return predict


def create_torch(options):
    """CPU MLP with fixed 3->16->1 architecture, preprocessing saved alongside weights."""
    import torch

    if set(options) != {"artifact", "mean", "scale"}:
        raise ValueError("torch options must contain artifact, mean and scale")
    mean = torch.tensor(options["mean"], dtype=torch.float32)
    scale = torch.tensor(options["scale"], dtype=torch.float32)
    if mean.shape != (3,) or scale.shape != (3,) or not bool(torch.isfinite(mean).all()) or not bool(torch.isfinite(scale).all()) or not bool((scale > 0).all()):
        raise ValueError("mean/scale must contain three finite values, scale > 0")
    net = torch.nn.Sequential(torch.nn.Linear(3, 16), torch.nn.ReLU(), torch.nn.Linear(16, 1))
    net.load_state_dict(torch.load(options["artifact"], map_location="cpu", weights_only=True))
    net.eval()

    def predict(inputs):
        with torch.inference_mode():
            features = torch.tensor([feature_vector(inputs)], dtype=torch.float32)
            return float(net((features - mean) / scale).item())

    return predict
