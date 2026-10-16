"""Image build inputs: Dockerfiles, weight baking, and the (mutating) build script. No Docker, no AWS."""

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

import hash_source
from adapter.bake import BakeError, bake
from hash_source import source_hash

DECIDER = Path(__file__).resolve().parents[1] / "infra" / "decider"
ADAPTER = DECIDER / "adapter"
SCRIPTS = DECIDER / "scripts"
MODEL = "Org/model"
# The one place the pinned model id is defined; every other copy is checked against it below.
PINNED_MODEL = re.search(r"DEFAULT_MODEL_ID = '([^']+)'", (DECIDER / "lib" / "names.ts").read_text()).group(1)
# The official Python image, mirrored on ECR Public (no login, no Docker Hub rate limit).
PYTHON_BASE = "public.ecr.aws/docker/library/python"
BASE_PATTERN = re.escape(PYTHON_BASE) + r":3\.1[1-9]\.\d+-slim-\w+@sha256:[0-9a-f]{64}$"
TORCH_INDEX = re.compile(r"--index-url https://download\.pytorch\.org/whl/cu12\d\b")
LOCK = ADAPTER / "requirements.lock"
LOCK_SOURCE = ADAPTER / "requirements.in"
TORCH_LOCK = ADAPTER / "torch.lock"


def lock_pins():
    """{normalized name: version} of every pinned entry in requirements.lock."""
    pins = {}
    for line in LOCK.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?: \\)?$", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def lock_entries():
    """{name: [lines of its block]}: each entry is its pin line plus the continuation lines that follow."""
    entries, current = {}, None
    for line in LOCK.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if match:
            current = match.group(1)
            entries[current] = []
        if current:
            entries[current].append(line)
    return entries


def version_tuple(version):
    return tuple(int(part) for part in re.match(r"\d+(?:\.\d+)*", version).group(0).split("."))


def base_line(lines):
    return next(line for line in lines if line.startswith("FROM "))


def instructions(path):
    """Dockerfile instructions with continuations joined and comments dropped."""
    lines, current = [], ""
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current += (" " if current else "") + stripped.removesuffix("\\").strip()
        if not stripped.endswith("\\"):
            lines.append(current)
            current = ""
    return lines


class TestProductionDockerfile:
    lines = instructions(ADAPTER / "Dockerfile")

    def test_builds_for_amd64_from_the_ecr_public_python_image_pinned_by_tag_and_digest(self):
        base = base_line(self.lines)
        assert "--platform=linux/amd64" in base
        assert re.search(BASE_PATTERN, base), base

    def test_python_is_3_11_or_newer(self):
        minor = int(re.search(r":3\.(\d+)\.", base_line(self.lines)).group(1))
        assert minor >= 11

    def test_pulls_nothing_from_docker_hub(self):
        for name in ("Dockerfile", "Dockerfile.local"):
            for line in instructions(ADAPTER / name):
                if line.startswith("FROM "):
                    assert line.split()[-1].startswith(f"{PYTHON_BASE}:"), line
            text = (ADAPTER / name).read_text().lower()
            assert "docker.io" not in text, name
            assert "hub.docker.com" not in text, name

    def test_has_no_syntax_directive_that_would_fetch_a_frontend_from_docker_hub(self):
        for name in ("Dockerfile", "Dockerfile.local"):
            assert "syntax=" not in (ADAPTER / name).read_text(), name

    def test_installs_torch_first_and_alone_from_a_cuda_12_wheel_index_at_the_locked_version(self):
        installs = [line for line in self.lines if "pip install" in line]
        first = installs[0]
        assert TORCH_INDEX.search(first), first  # cu12x: a default PyPI pull may be CUDA 13
        assert "--no-deps" in first  # its nvidia-* libraries come from the lock, hash-checked
        assert "--require-hashes" in first  # pip skips torch's entry in the main lock: verify it here
        assert re.search(r"(-r|--requirement) /tmp/torch\.lock\b", first), first
        assert not re.findall(r'"[^"]+"', first), first
        pin = TORCH_LOCK.read_text().splitlines()[0]
        assert pin.startswith(f"torch=={lock_pins()['torch']} "), pin
        assert re.search(r"\+cu12\d ", pin)

    def test_the_torch_first_file_carries_exactly_the_torch_hashes_of_the_lock(self):
        def hashes(lines):
            return sorted(re.findall(r"--hash=sha256:([0-9a-f]{64})", "\n".join(lines)))

        assert hashes(TORCH_LOCK.read_text().splitlines()) == hashes(lock_entries()["torch"])
        assert hashes(lock_entries()["torch"])
        assert TORCH_LOCK.read_text().count("==") == 1  # a single requirement
        copy_at = next(
            i for i, line in enumerate(self.lines) if line.startswith("COPY ") and "torch.lock" in line
        )
        first_install = next(i for i, line in enumerate(self.lines) if "pip install" in line)
        assert copy_at < first_install

    def test_every_other_install_reads_the_hash_locked_requirements_and_nothing_inline(self):
        installs = [line for line in self.lines if "pip install" in line]
        assert len(installs) == 2
        for line in installs[1:]:
            assert "--require-hashes" in line, line
            assert "--no-deps" in line, line  # the lock is the full closure: nothing resolved at build time
            assert re.search(r"(-r|--requirement) /tmp/requirements\.lock\b", line), line
            assert not re.findall(r'"[^"]+"', line), line  # no inline requirement bypassing the lock
        copy_at = next(i for i, line in enumerate(self.lines) if line.startswith("COPY requirements.lock "))
        install_at = next(i for i, line in enumerate(self.lines) if "--require-hashes" in line)
        assert copy_at < install_at
        inline = r"(strands-decider|transformers|fastapi|uvicorn|httpx)=="
        assert not any(re.search(inline, line) for line in self.lines)

    def test_the_locked_install_uses_the_same_indexes_the_lock_was_compiled_with(self):
        lock_install = next(line for line in self.lines if "/tmp/requirements.lock" in line and "pip" in line)
        assert "--index-url https://pypi.org/simple" in lock_install
        assert re.search(r"--extra-index-url https://download\.pytorch\.org/whl/cu12\d\b", lock_install)
        header = LOCK_SOURCE.read_text()
        assert "--index-url https://pypi.org/simple" in header
        assert re.search(r"--extra-index-url https://download\.pytorch\.org/whl/cu12\d\b", header)

    def test_torch_is_installed_before_everything_else_and_never_from_a_cuda_13_or_nightly_index(self):
        installs = [line for line in self.lines if "pip install" in line]
        assert "/tmp/torch.lock" in installs[0]
        assert not any("whl/cu13" in line or "whl/nightly" in line for line in self.lines)

    def test_the_lock_pins_every_package_with_a_hash(self):
        entries = lock_entries()
        assert entries
        for name, block in entries.items():
            assert re.match(rf"^{re.escape(name)}==\S+ \\$", block[0]), name
            assert any("--hash=sha256:" in line for line in block), name

    def test_the_lock_holds_the_runtime_dependencies_on_a_cuda_12_torch(self):
        pins = lock_pins()
        for name in (
            "torch",
            "transformers",
            "strands-decider",
            "peft",
            "accelerate",
            "safetensors",
            "tokenizers",
            "huggingface-hub",
            "fastapi",
            "uvicorn",
            "httpx",
        ):
            assert name in pins, name
        assert re.search(r"\+cu12\d$", pins["torch"]), pins["torch"]
        assert pins["strands-decider"] == "0.1.0"

    def test_the_lock_has_no_cuda_13_or_cpu_wheels(self):
        text = LOCK.read_text()
        assert not re.search(r"cu13\d|\+cpu", text)
        assert all(name.endswith("-cu12") for name in lock_pins() if name.startswith("nvidia-"))

    def test_the_locked_versions_satisfy_what_strands_decider_asks_for(self):
        pins = lock_pins()
        # torch.distributed.checkpoint.hf_storage first ships in 2.8
        assert version_tuple(pins["torch"]) >= (2, 8)
        assert (5, 15) <= version_tuple(pins["transformers"]) < (6,)
        assert version_tuple(pins["peft"]) >= (0, 21)
        assert version_tuple(pins["huggingface-hub"]) >= (1, 5)

    def test_the_lock_source_pins_the_same_torch_as_the_lock(self):
        source = LOCK_SOURCE.read_text()
        assert f"torch=={lock_pins()['torch']}" in source
        assert f"transformers=={lock_pins()['transformers']}" in source

    def test_a_smoke_import_runs_last_after_the_installs_the_bake_and_the_offline_switch(self):
        runs = [i for i, line in enumerate(self.lines) if line.startswith("RUN ")]
        smoke_at = next(i for i, line in enumerate(self.lines) if "adapter.smoke" in line)
        assert smoke_at == runs[-1]  # the last RUN
        install_at = max(i for i, line in enumerate(self.lines) if "pip install" in line)
        bake_at = next(i for i, line in enumerate(self.lines) if "adapter.bake" in line)
        offline_at = next(i for i, line in enumerate(self.lines) if "HF_HUB_OFFLINE=1" in line)
        assert install_at < bake_at < offline_at < smoke_at
        assert self.lines[smoke_at].startswith("RUN python -m adapter.smoke")

    def test_the_image_smoke_is_strict_so_codebuild_can_never_skip_the_meta_device_build(self):
        smoke_line = next(line for line in self.lines if "adapter.smoke" in line)
        assert "--require-checkpoint" in smoke_line

    def test_has_no_dlc_leftovers(self):
        text = (ADAPTER / "Dockerfile").read_text()
        assert "dockerd-entrypoint" not in text
        assert "torchserve" not in text.lower()
        assert "763104351884" not in text
        entrypoint = [line for line in self.lines if line.startswith("ENTRYPOINT ")]
        cmd = [line for line in self.lines if line.startswith("CMD ")]
        assert len(entrypoint) == 1
        assert len(cmd) == 1

    def test_does_not_bundle_nvidia_drivers(self):
        text = (ADAPTER / "Dockerfile").read_text().lower()
        assert "nvidia-driver" not in text
        assert not re.search(r"(apt-get|apt|yum|dnf) .*nvidia", text)

    def test_is_offline_at_runtime(self):
        env = " ".join(line for line in self.lines if line.startswith("ENV "))
        assert "HF_HUB_OFFLINE=1" in env
        # Offline must be switched on AFTER the weights are downloaded, never before.
        bake_at = next(i for i, line in enumerate(self.lines) if "adapter.bake" in line)
        offline_at = next(i for i, line in enumerate(self.lines) if "HF_HUB_OFFLINE=1" in line)
        assert bake_at < offline_at

    def test_defaults_the_dtype_to_fp16(self):
        assert any("DECIDER_DTYPE=float16" in line for line in self.lines)

    def test_fails_the_build_when_the_checkpoint_has_no_dtype_key(self):
        text = " ".join(self.lines)
        assert "adapter.bake" in text  # bake() runs the same check as `python -m adapter.stage --check`

    def test_uses_the_exec_form_entrypoint_that_accepts_serve(self):
        entrypoint = next(line for line in self.lines if line.startswith("ENTRYPOINT "))
        assert json.loads(entrypoint.removeprefix("ENTRYPOINT ")) == ["python", "-m", "adapter"]
        cmd = next(line for line in self.lines if line.startswith("CMD "))
        assert json.loads(cmd.removeprefix("CMD ")) == ["serve"]

    def test_exposes_the_sagemaker_port(self):
        assert "EXPOSE 8080" in self.lines


class TestLocalDockerfile:
    lines = instructions(ADAPTER / "Dockerfile.local")

    def test_has_no_torch_and_no_weights(self):
        text = " ".join(self.lines).lower()
        for forbidden in ("torch", "strands-decider", "huggingface", "adapter.bake"):
            assert forbidden not in text

    def test_runs_the_same_entrypoint_against_the_fake_decider(self):
        entrypoint = next(line for line in self.lines if line.startswith("ENTRYPOINT "))
        assert json.loads(entrypoint.removeprefix("ENTRYPOINT ")) == ["python", "-m", "adapter"]
        env = [line for line in self.lines if "DECIDER_UPSTREAM_COMMAND=" in line]
        assert env
        assert "adapter.fake_decider" in env[0]

    def test_uses_the_same_ecr_public_python_base_as_the_production_image(self):
        assert re.search(BASE_PATTERN, base_line(self.lines)), base_line(self.lines)
        assert base_line(self.lines).split()[-1] == base_line(TestProductionDockerfile.lines).split()[-1]

    def test_pins_its_requirements(self):
        for line in (line for line in self.lines if "pip install" in line):
            assert all("==" in requirement for requirement in re.findall(r'"([^"]+)"', line)), line


class FakeModules:
    """Stands in for importlib.import_module: a dict of fake modules; missing names raise like an import."""

    def __init__(self, **modules):
        self.modules = modules
        self.imported = []

    def __call__(self, name):
        self.imported.append(name)
        if name not in self.modules:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return self.modules[name]


def healthy_modules(**overrides):
    from types import SimpleNamespace

    names = [
        "torch",
        "transformers",
        "strands_decider",
        "peft",
        "accelerate",
        "huggingface_hub",
        "safetensors",
        "fastapi",
        "uvicorn",
        "httpx",
    ]
    modules = {name: SimpleNamespace(__version__="1.2.3") for name in names}
    modules["transformers"] = SimpleNamespace(__version__="5.19.0", Qwen3_5ForCausalLM=object)
    modules["strands_decider.modeling"] = SimpleNamespace(StrandsDeciderModel=object)
    modules["torch.distributed.checkpoint.hf_storage"] = SimpleNamespace()
    modules.update(overrides)
    return FakeModules(**modules)


class TestSmoke:
    """`python -m adapter.smoke` is the last RUN of the image: a broken dependency set must fail the build."""

    def run(self, imports, checkpoint=None, architecture=lambda directory: "Qwen3_5ForCausalLM"):
        from adapter.smoke import smoke

        lines = []
        smoke(checkpoint, import_module=imports, load_architecture=architecture, report=lines.append)
        return "\n".join(lines)

    def test_imports_the_model_stack_and_prints_the_versions(self):
        imports = healthy_modules()
        out = self.run(imports)
        for name in ("torch", "transformers", "strands_decider"):
            assert name in imports.imported
        assert "transformers 5.19.0" in out
        assert "torch 1.2.3" in out

    def test_resolves_the_model_class_the_decider_loads_without_weights(self):
        imports = healthy_modules()
        self.run(imports)
        assert "torch.distributed.checkpoint.hf_storage" in imports.imported

    def test_the_incident_fails_the_build_a_transformers_that_cannot_import_its_model_class(self):
        from adapter.smoke import SmokeError

        class Broken:
            __version__ = "5.20.1"

            def __getattr__(self, name):
                raise ModuleNotFoundError("No module named 'torch.distributed.checkpoint.hf_storage'")

        with pytest.raises(SmokeError, match="Qwen3_5ForCausalLM"):
            self.run(healthy_modules(transformers=Broken()))

    def test_a_torch_without_hf_storage_fails(self):
        from adapter.smoke import SmokeError

        imports = healthy_modules()
        del imports.modules["torch.distributed.checkpoint.hf_storage"]
        with pytest.raises(SmokeError, match="hf_storage"):
            self.run(imports)

    @pytest.mark.parametrize("missing", ["torch", "transformers", "strands_decider", "peft", "fastapi"])
    def test_a_missing_package_fails_and_names_it(self, missing):
        from adapter.smoke import SmokeError

        imports = healthy_modules()
        del imports.modules[missing]
        with pytest.raises(SmokeError, match=missing):
            self.run(imports)

    def test_imports_the_module_the_decider_serve_path_uses_to_build_its_model(self):
        imports = healthy_modules()
        self.run(imports)
        assert "strands_decider.modeling" in imports.imported

    def test_a_decider_modeling_module_that_cannot_import_fails(self):
        from adapter.smoke import SmokeError

        imports = healthy_modules()
        del imports.modules["strands_decider.modeling"]
        with pytest.raises(SmokeError, match="strands_decider.modeling"):
            self.run(imports)

    def test_a_decider_modeling_module_without_the_model_class_fails(self):
        from types import SimpleNamespace

        from adapter.smoke import SmokeError

        imports = healthy_modules(**{"strands_decider.modeling": SimpleNamespace()})
        with pytest.raises(SmokeError, match="StrandsDeciderModel"):
            self.run(imports)

    def test_strict_mode_without_a_checkpoint_fails(self):
        from adapter.smoke import SmokeError, smoke

        with pytest.raises(SmokeError, match="checkpoint"):
            smoke(None, require_checkpoint=True, import_module=healthy_modules(), report=lambda line: None)

    def test_without_a_checkpoint_it_skips_the_config_check_but_still_checks_the_class(self):
        out = self.run(healthy_modules(), checkpoint=None)
        assert "skipped" in out

    def test_with_a_checkpoint_the_architecture_is_resolved_from_its_config(self, tmp_path):
        seen = []
        out = self.run(
            healthy_modules(),
            checkpoint=tmp_path,
            architecture=lambda directory: seen.append(directory) or "Qwen3_5ForCausalLM",
        )
        assert seen == [tmp_path]
        assert "Qwen3_5ForCausalLM" in out

    def test_an_architecture_the_installed_transformers_lacks_fails(self, tmp_path):
        from adapter.smoke import SmokeError

        with pytest.raises(SmokeError, match="Qwen9ForCausalLM"):
            self.run(
                healthy_modules(), checkpoint=tmp_path, architecture=lambda directory: "Qwen9ForCausalLM"
            )

    @pytest.mark.parametrize("how", ["flag", "env"])
    def test_the_cli_in_strict_mode_exits_1_when_the_checkpoint_dir_is_unset(self, monkeypatch, capsys, how):
        from adapter import smoke

        monkeypatch.delenv("DECIDER_CHECKPOINT_DIR", raising=False)
        monkeypatch.setattr(smoke, "smoke", lambda *a, **k: pytest.fail("must not run without a checkpoint"))
        argv = ["--require-checkpoint"] if how == "flag" else []
        if how == "env":
            monkeypatch.setenv("SMOKE_REQUIRE_CHECKPOINT", "1")
        assert smoke.main(argv) == 1
        assert "checkpoint" in capsys.readouterr().err

    def test_the_cli_in_strict_mode_exits_1_when_the_config_is_missing(self, monkeypatch, tmp_path, capsys):
        from adapter import smoke

        monkeypatch.setenv("DECIDER_CHECKPOINT_DIR", str(tmp_path))  # no strands_decider_config.json in it
        monkeypatch.setattr(smoke, "smoke", lambda *a, **k: pytest.fail("must not run without a config"))
        assert smoke.main(["--require-checkpoint"]) == 1
        assert "strands_decider_config.json" in capsys.readouterr().err

    def test_the_cli_in_strict_mode_passes_with_a_fake_checkpoint(self, monkeypatch, tmp_path, capsys):
        from adapter import smoke

        (tmp_path / "strands_decider_config.json").write_text("{}")
        monkeypatch.setenv("DECIDER_CHECKPOINT_DIR", str(tmp_path))
        seen = []
        monkeypatch.setattr(smoke, "smoke", lambda checkpoint, **kwargs: seen.append((checkpoint, kwargs)))
        assert smoke.main(["--require-checkpoint"]) == 0
        assert seen[0][0] == tmp_path
        assert "smoke check passed" in capsys.readouterr().out

    def test_the_cli_without_strict_mode_still_runs_with_no_checkpoint(self, monkeypatch, capsys):
        from adapter import smoke

        monkeypatch.delenv("DECIDER_CHECKPOINT_DIR", raising=False)
        monkeypatch.delenv("SMOKE_REQUIRE_CHECKPOINT", raising=False)
        seen = []
        monkeypatch.setattr(smoke, "smoke", lambda checkpoint, **kwargs: seen.append(checkpoint))
        assert smoke.main([]) == 0
        assert seen == [None]

    def test_the_cli_returns_non_zero_when_the_stack_is_broken(self, monkeypatch, capsys):
        from adapter import smoke

        def boom(*args, **kwargs):
            raise smoke.SmokeError("torch is broken")

        monkeypatch.setattr(smoke, "smoke", boom)
        assert smoke.main([]) == 1
        assert "torch is broken" in capsys.readouterr().err


class FakeHub:
    """Stands in for huggingface_hub.snapshot_download."""

    def __init__(self, config):
        self.config = config
        self.calls = []

    def __call__(self, repo_id, *, revision, local_dir=None, cache_dir=None):
        self.calls.append(
            {"repo_id": repo_id, "revision": revision, "local_dir": local_dir, "cache_dir": cache_dir}
        )
        if local_dir is not None:
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "strands_decider_config.json").write_text(json.dumps(self.config))
            return str(local_dir)
        folder = Path(cache_dir) / f"models--{repo_id.replace('/', '--')}" / "snapshots" / revision
        folder.mkdir(parents=True)
        return str(folder)


class TestBake:
    def run(self, tmp_path, config=None, **kwargs):
        hub = FakeHub(config or {"base_model": "Qwen/Base", "torch_dtype": "bfloat16"})
        bake(
            model_id=MODEL,
            model_revision="m" * 40,
            base_revision="b" * 40,
            checkpoint_dir=tmp_path / "ckpt",
            hf_home=tmp_path / "hf",
            download=hub,
            **kwargs,
        )
        return hub

    def test_downloads_the_checkpoint_then_the_base_model_it_names(self, tmp_path):
        hub = self.run(tmp_path)
        assert [c["repo_id"] for c in hub.calls] == [MODEL, "Qwen/Base"]
        assert [c["revision"] for c in hub.calls] == ["m" * 40, "b" * 40]
        assert hub.calls[0]["local_dir"] == tmp_path / "ckpt"
        assert hub.calls[1]["cache_dir"] == tmp_path / "hf" / "hub"

    def test_points_main_at_the_pinned_base_revision_so_offline_loading_resolves(self, tmp_path):
        self.run(tmp_path)
        ref = tmp_path / "hf" / "hub" / "models--Qwen--Base" / "refs" / "main"
        assert ref.read_text() == "b" * 40

    def test_fails_when_the_checkpoint_has_no_dtype_key(self, tmp_path):
        with pytest.raises(BakeError, match="torch_dtype"):
            self.run(tmp_path, config={"base_model": "Qwen/Base"})

    def test_fails_when_the_checkpoint_names_no_base_model(self, tmp_path):
        with pytest.raises(BakeError, match="base_model"):
            self.run(tmp_path, config={"torch_dtype": "bfloat16"})

    def test_expected_base_model_mismatch_fails_instead_of_baking_the_wrong_weights(self, tmp_path):
        with pytest.raises(BakeError, match="Other/Base"):
            self.run(tmp_path, expected_base_model="Other/Base")


class TestPinnedModelId:
    def test_the_dockerfile_default_matches_the_single_definition(self):
        text = (ADAPTER / "Dockerfile").read_text()
        assert re.search(r"^ARG MODEL_ID=(\S+)$", text, re.MULTILINE).group(1) == PINNED_MODEL

    def test_cdk_json_does_not_carry_a_second_copy(self):
        context = json.loads((DECIDER / "cdk.json").read_text())["context"]
        assert context.get("modelId", PINNED_MODEL) == PINNED_MODEL

    def test_the_build_script_reads_it_from_the_single_definition(self):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "build_image.sh")], capture_output=True, text=True, timeout=30
        )
        assert f"model id:    {PINNED_MODEL}" in result.stdout
        assert PINNED_MODEL not in (SCRIPTS / "build_image.sh").read_text()


def asset_excludes():
    """The exclude globs CtfPersistentStack gives the S3 asset (what CodeBuild actually receives)."""
    text = (DECIDER / "lib" / "persistent-stack.ts").read_text()
    block = re.search(r"exclude:\s*\[([^\]]*)\]", text).group(1)
    return re.findall(r"'([^']+)'", block)


def zip_like_the_asset(source: Path, target: Path, excludes: list[str]) -> None:
    def excluded(relative: Path) -> bool:
        return any(
            fnmatch.fnmatch(part, pattern.removeprefix("**/"))
            for pattern in excludes
            for part in relative.parts
        )

    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_file() and not excluded(relative):
                archive.write(path, relative.as_posix())


class TestHashParityWithTheUploadedSource:
    """CodeBuild hashes an UNZIPPED copy of the S3 asset; build_image.sh hashes the directory."""

    def unzip(self, tmp_path, excludes):
        archive, out = tmp_path / "asset.zip", tmp_path / "unzipped"
        zip_like_the_asset(ADAPTER, archive, excludes)
        with zipfile.ZipFile(archive) as z:
            z.extractall(out)
        return out

    def test_the_directory_and_its_unzipped_asset_hash_the_same(self, tmp_path):
        unzipped = self.unzip(tmp_path, asset_excludes())
        assert source_hash(unzipped, PINNED_MODEL) == source_hash(ADAPTER, PINNED_MODEL)

    def test_stray_caches_in_the_working_copy_do_not_change_the_hash(self, tmp_path):
        clean = self.unzip(tmp_path, asset_excludes())
        dirty = tmp_path / "dirty"
        shutil.copytree(clean, dirty)
        (dirty / "adapter" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (dirty / "adapter" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"junk")
        (dirty / ".pytest_cache" / "v").mkdir(parents=True)
        (dirty / ".pytest_cache" / "v" / "lastfailed").write_text("{}")
        (dirty / "adapter" / "stray.pyc").write_bytes(b"junk")
        assert source_hash(dirty, PINNED_MODEL) == source_hash(clean, PINNED_MODEL)

    def test_an_asset_that_kept_the_caches_would_still_hash_the_same(self, tmp_path):
        unzipped = self.unzip(tmp_path, excludes=[])
        assert source_hash(unzipped, PINNED_MODEL) == source_hash(ADAPTER, PINNED_MODEL)

    def test_the_asset_excludes_and_the_hash_exclusions_are_the_same_set(self):
        directories = {p.removeprefix("**/") for p in asset_excludes() if "*" not in p.removeprefix("**/")}
        suffixes = {
            p.removeprefix("**/*") for p in asset_excludes() if p.removeprefix("**/").startswith("*.")
        }
        assert directories == hash_source.EXCLUDED_DIRS
        assert suffixes == hash_source.EXCLUDED_SUFFIXES

    def test_a_changed_file_changes_the_hash_on_both_sides(self, tmp_path):
        unzipped = self.unzip(tmp_path, asset_excludes())
        before = source_hash(unzipped, PINNED_MODEL)
        (unzipped / "Dockerfile").write_text((unzipped / "Dockerfile").read_text() + "\n# edited\n")
        assert source_hash(unzipped, PINNED_MODEL) != before


def fake_aws(
    tmp_path, *, image_exists=False, build_status="SUCCEEDED", describe_error="ImageNotFoundException"
):
    """A fake `aws` on PATH that records every call and answers the few the script makes."""
    log = tmp_path / "aws.log"
    message = f"An error occurred ({describe_error}) when calling the DescribeImages operation: x"
    not_found = f'echo "{message}" >&2; exit 254'
    binary = tmp_path / "bin" / "aws"
    binary.parent.mkdir()
    binary.write_text(
        f"""#!/usr/bin/env bash
echo "$*" >> "{log}"
case "$1 $2" in
  "ssm get-parameter")
    case "$4" in
      /ctf-decider/build-project) echo "ctf-build-project";;
      /ctf-decider/repo-uri) echo "000000000000.dkr.ecr.eu-west-1.amazonaws.com/ctf-decider";;
      *) exit 254;;
    esac;;
  "ecr describe-images")
    {"exit 0" if image_exists else not_found};;
  "codebuild start-build") echo "ctf-build-project:1234";;
  "codebuild batch-get-builds") echo "{build_status}";;
  *) echo "unexpected aws call: $*" >&2; exit 99;;
esac
"""
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary.parent, log


def run_script(tmp_path, *args, **aws_options):
    bin_dir, log = fake_aws(tmp_path, **aws_options)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POLL_SECONDS": "0",
        "AWS_REGION": "eu-west-1",
    }
    result = subprocess.run(
        ["bash", str(SCRIPTS / "build_image.sh"), *args], capture_output=True, text=True, env=env, timeout=60
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


class TestBuildImageScript:
    expected_tag = source_hash(ADAPTER, PINNED_MODEL)

    def test_is_syntactically_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPTS / "build_image.sh")], check=True)

    def test_without_yes_it_only_prints_the_plan_and_calls_no_aws(self, tmp_path):
        result, calls = run_script(tmp_path)
        assert result.returncode != 0
        assert "--yes" in result.stderr + result.stdout
        assert self.expected_tag in result.stdout
        assert calls == []

    def test_the_tag_is_the_content_hash_of_the_adapter_plus_the_pinned_model_id(self, tmp_path):
        result, calls = run_script(tmp_path)
        assert f"image tag:   {self.expected_tag}" in result.stdout
        assert calls == []

    def test_the_model_id_cannot_be_overridden_from_the_command_line(self, tmp_path):
        result, calls = run_script(tmp_path, "--yes", "--model-id", "Org/other")
        assert result.returncode != 0
        assert calls == []  # no tag for another model can be built from here

    def test_with_yes_it_starts_the_build_with_the_tag_and_waits_for_success(self, tmp_path):
        result, calls = run_script(tmp_path, "--yes")
        assert result.returncode == 0, result.stderr
        start = next(call for call in calls if call.startswith("codebuild start-build"))
        assert "ctf-build-project" in start
        assert f"IMAGE_TAG,value={self.expected_tag}" in start
        assert any(call.startswith("codebuild batch-get-builds") for call in calls)
        assert f"imageTag={self.expected_tag}" in result.stdout

    def test_it_skips_the_build_when_the_tag_is_already_in_ecr(self, tmp_path):
        result, calls = run_script(tmp_path, "--yes", image_exists=True)
        assert result.returncode == 0
        assert not any(call.startswith("codebuild start-build") for call in calls)
        assert f"imageTag={self.expected_tag}" in result.stdout

    def test_only_image_not_found_means_build(self, tmp_path):
        result, calls = run_script(tmp_path, "--yes", describe_error="ImageNotFoundException")
        assert result.returncode == 0, result.stderr
        assert any(call.startswith("codebuild start-build") for call in calls)

    @pytest.mark.parametrize(
        "error", ["AccessDeniedException", "RepositoryNotFoundException", "ThrottlingException"]
    )
    def test_any_other_ecr_error_fails_loudly_and_starts_no_build(self, tmp_path, error):
        result, calls = run_script(tmp_path, "--yes", describe_error=error)
        assert result.returncode != 0
        assert error in result.stderr
        assert not any(call.startswith("codebuild start-build") for call in calls)

    def test_a_failed_build_exits_non_zero(self, tmp_path):
        result, _ = run_script(tmp_path, "--yes", build_status="FAILED")
        assert result.returncode != 0
        assert "FAILED" in result.stderr + result.stdout

    def test_unknown_flags_are_rejected_before_any_aws_call(self, tmp_path):
        result, calls = run_script(tmp_path, "--yes", "--bogus")
        assert result.returncode != 0
        assert calls == []


def make(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k not in {"IMAGE_TAG", "CTF_BUDGET_EMAIL"}}
    return subprocess.run(
        ["make", "-n", "-C", str(DECIDER), *args], capture_output=True, text=True, env={**base, **(env or {})}
    )


class TestMakefile:
    """`make -n` prints the commands without running them: nothing here can touch AWS."""

    def test_destroy_needs_no_tag_and_no_email(self):
        result = make("destroy-decider")
        assert result.returncode == 0, result.stderr
        assert "cdk destroy DeciderStack" in result.stdout
        assert "imageTag" not in result.stdout
        assert "CtfPersistentStack" not in result.stdout  # the persistent stack is never destroyed here

    def test_deploying_the_decider_requires_a_tag_and_passes_it_as_context(self):
        missing = make("deploy-decider")
        assert missing.returncode != 0
        assert "IMAGE_TAG" in missing.stderr
        given = make("deploy-decider", env={"IMAGE_TAG": "abc123"})
        assert given.returncode == 0, given.stderr
        assert "cdk deploy DeciderStack -c imageTag=abc123" in given.stdout

    def test_deploying_the_persistent_stack_requires_the_budget_email(self):
        missing = make("deploy-persistent")
        assert missing.returncode != 0
        assert "CTF_BUDGET_EMAIL" in missing.stderr
        given = make("deploy-persistent", env={"CTF_BUDGET_EMAIL": "me@example.invalid"})
        assert given.returncode == 0, given.stderr
        assert "cdk deploy CtfPersistentStack" in given.stdout
        assert "me@example.invalid" not in given.stdout  # read from the environment, not echoed into argv


class TestHygiene:
    def test_no_account_ids_emails_or_home_paths_in_the_infra_tree(self):
        offenders = []
        for path in DECIDER.rglob("*"):
            skip = {"node_modules", "cdk.out", "dist", "__pycache__"}
            if not path.is_file() or skip & set(path.parts) or path.name == "package-lock.json":
                continue
            text = path.read_text(errors="ignore")
            for pattern in (r"\b\d{12}\b", r"[\w.+-]+@(?!example\.invalid)[\w-]+\.[a-z]{2,}", r"/Users/"):
                for match in re.finditer(pattern, text):
                    if match.group(0) != "000000000000":
                        offenders.append((path.name, match.group(0)))
        assert offenders == []
