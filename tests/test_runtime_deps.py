"""Runtime dependency audit (spec ctf-agent-runtimes: no torch in runtimes; harness pins strands-harness)."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = ("harness", "harness_sdk", "strand_decider", "decider_loop")
FORBIDDEN = (
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "triton",
    "safetensors",
    "strands-decider",
    "nvidia",
)


def _sources(base):
    """Python files under `base`, skipping virtualenvs and bytecode caches."""
    return [p for p in base.rglob("*.py") if not {".venv", "__pycache__"} & set(p.relative_to(base).parts)]


def dependencies(runtime):
    data = tomllib.loads((ROOT / "app" / runtime / "pyproject.toml").read_text())
    return data["project"]["dependencies"]


def names(deps):
    return {re.split(r"[\s\[<>=~!;]", dep, maxsplit=1)[0].lower().replace("_", "-") for dep in deps}


@pytest.mark.parametrize("runtime", RUNTIMES)
class TestPerRuntime:
    def test_no_model_library_is_declared(self, runtime):
        declared = names(dependencies(runtime))
        assert declared  # the audit looked at something
        assert not {d for d in declared if d.startswith(FORBIDDEN)}

    def test_the_sagemaker_adapter_stack_stays_out_of_the_runtimes(self, runtime):
        """FastAPI/uvicorn belong to infra/decider/adapter (its Dockerfile pins them), not to a CodeZip."""
        declared = names(dependencies(runtime))
        assert not {d for d in declared if d.startswith(("fastapi", "uvicorn"))}
        for path in _sources(ROOT / "app" / runtime):
            assert not re.search(r"^\s*(import|from)\s+adapter\b", path.read_text(), re.M), path

    def test_python_matches_the_runtime_version(self, runtime):
        data = tomllib.loads((ROOT / "app" / runtime / "pyproject.toml").read_text())
        assert data["project"]["requires-python"] == ">=3.13"

    def test_runtime_sources_never_import_a_model_library(self, runtime):
        for path in _sources(ROOT / "app" / runtime):
            text = path.read_text()
            assert not re.search(r"^\s*(import|from)\s+(torch|transformers|strands_decider)\b", text, re.M), (
                path
            )

    def test_every_runtime_declares_the_shared_code_it_imports(self, runtime):
        assert (
            ROOT / "app" / runtime / "ctf_common" / "decider.py"
        ).is_file()  # vendored, not a path dependency


def test_only_the_harness_depends_on_strands_harness_and_it_pins_the_floor():
    for runtime in RUNTIMES:
        deps = dependencies(runtime)
        has = any(d.lower().startswith("strands-harness") for d in deps)
        assert has is (runtime == "harness"), runtime
    pin = next(d for d in dependencies("harness") if d.lower().startswith("strands-harness"))
    assert re.search(r">=\s*0\.3\.0", pin)  # the resolver otherwise picks 0.0.0


def test_the_shared_package_is_free_of_model_libraries():
    for path in _sources(ROOT / "shared" / "ctf_common"):
        assert not re.search(r"^\s*(import|from)\s+(torch|transformers)\b", path.read_text(), re.M), path
