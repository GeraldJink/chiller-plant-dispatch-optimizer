"""Build an explicit public-source archive, excluding all local data and caches."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
PUBLIC_FILES = (
    "README.md", "CONTRIBUTING.md", "LICENSE", "requirements.txt",
    ".gitignore", ".gitattributes", ".github/workflows/tests.yml",
    "config.py", "config/default.json", "power_models.py", "pump_lookup.py",
    "optimize_utils.py", "ga_series.py", "load_generate.py", "main.py",
    "create_pump_tables.py", "build_release.py", "examples/load.csv",
    "tests/test_planner.py",
    "model_interface.py", "docs/MODEL_GUIDE.md", "examples/__init__.py",
    "examples/model_adapters.py", "examples/use_custom_model.py", "tests/test_model_interface.py",
    "load_allocation.py", "chiller_scheduling.py", "examples/load_allocators.py",
    "docs/CHILLER_SCHEDULING.md", "tests/test_chiller_scheduling.py",
)


def build_release(path):
    path = Path(path).resolve()
    sources = [(name, ROOT / name) for name in PUBLIC_FILES]
    if path in {source.resolve() for _, source in sources}:
        raise ValueError("Archive must not overwrite a source file")
    for name, source in sources:
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError(f"Missing or unsafe public source: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, source in sources:
            archive.write(source, arcname=f"forecast_and_plan_simple/{name}")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="outputs/forecast_and_plan_simple-source.zip")
    args = parser.parse_args()
    try:
        print(build_release(args.output))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
