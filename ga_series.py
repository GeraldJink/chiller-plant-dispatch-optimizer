"""Real-coded, whole-horizon genetic search: tournament, crossover, mutation, elites."""
import math
import random
from optimize_utils import Plant, validate_schedule
from config import validate_config
from load_generate import validate_load


class SeriesGA:
    def __init__(self, cfg):
        validate_config(cfg)
        self.cfg = cfg

    def optimize(self, loads, progress=None):
        validate_load(loads)
        plant = Plant(self.cfg, loads)
        opt = self.cfg["optimization"]
        rng = random.Random(opt["seed"])
        width = len(loads) * 4
        # Include deterministic, physically interpretable starting schedules.
        seeds = [[v] * width for v in (0.5, 0.25, 0.75, 0.0, 1.0)]
        population = seeds[:opt["population_size"]]
        population += [[rng.random() for _ in range(width)] for _ in range(opt["population_size"] - len(population))]
        ranked = sorted([(plant.evaluate(g)[0], g) for g in population], key=lambda pair: pair[0])
        initial_best = ranked[0][0]
        history = []
        for generation in range(opt["generations"] + 1):
            best = ranked[0][0]
            history.append({"generation": generation, "best_energy_kwh": best if math.isfinite(best) else None})
            if progress:
                progress(generation, best)
            if generation == opt["generations"]:
                break

            def select():
                return min(rng.sample(ranked, opt["tournament_size"]), key=lambda pair: pair[0])[1]

            next_ranked = ranked[:opt["elite_count"]]
            while len(next_ranked) < opt["population_size"]:
                parent_a, parent_b = select(), select()
                child = parent_a.copy()
                if rng.random() < opt["crossover_rate"]:
                    # A blend per gene allows the sequence to evolve jointly.
                    for j in range(width):
                        weight = rng.random()
                        child[j] = weight * parent_a[j] + (1 - weight) * parent_b[j]
                for j in range(width):
                    if rng.random() < opt["mutation_rate"]:
                        child[j] = max(0.0, min(1.0, child[j] + rng.gauss(0, opt["mutation_scale"])))
                # Inject diversity if the entire population is infeasible.
                if not math.isfinite(best):
                    child = [rng.random() for _ in range(width)]
                next_ranked.append((plant.evaluate(child)[0], child))
            ranked = sorted(next_ranked, key=lambda pair: pair[0])
        if not math.isfinite(ranked[0][0]):
            raise ValueError("No feasible schedule found. Check load, pump minimum flow/capacity, chiller minimum PLR and predictor domain, initial temperatures, wet-bulb and search bounds; or increase GA population/generations. This is not a proof of infeasibility.")
        energy, schedule = plant.evaluate(ranked[0][1], detailed=True)
        validate_schedule(self.cfg, loads, schedule, chiller_models=plant.chiller_models)
        return {"algorithm": "genetic_algorithm", "status": "feasible_best_found", "seed": opt["seed"], "total_energy_kwh": energy, "initial_population_best_energy_kwh": initial_best if math.isfinite(initial_best) else None, "history": history, "schedule": schedule}
