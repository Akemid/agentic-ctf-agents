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

    def test_installs_torch_2_7_1_from_a_cuda_12_wheel_index(self):
        installs = [line for line in self.lines if "pip install" in line and '"torch==' in line]
        assert len(installs) == 1
        assert re.search(r'"torch==2\.7\.1"', installs[0])
        assert TORCH_INDEX.search(installs[0]), installs[0]  # cu12x: a default PyPI pull may be CUDA 13

    def test_torch_is_installed_before_everything_else_and_pip_may_not_replace_it(self):
        installs = [line for line in self.lines if "pip install" in line]
        assert '"torch==' in installs[0]
        for line in installs[1:]:
            assert re.search(r"(-c|--constraint) \S+", line), line
            assert "torch==2.7.1+cu12" in " ".join(self.lines)  # the constraint names the local version
        assert not any("whl/cu13" in line or "whl/nightly" in line for line in self.lines)

    def test_has_no_dlc_leftovers(self):
        text = (ADAPTER / "Dockerfile").read_text()
        assert "dockerd-entrypoint" not in text
        assert "torchserve" not in text.lower()
        assert "763104351884" not in text
        entrypoint = [line for line in self.lines if line.startswith("ENTRYPOINT ")]
        cmd = [line for line in self.lines if line.startswith("CMD ")]
        assert len(entrypoint) == 1
        assert len(cmd) == 1

    def test_pins_every_pip_requirement(self):
        installs = [line for line in self.lines if "pip install" in line]
        assert installs
        for line in installs:
            requirements = re.findall(r'"([^"]+)"', line)
            assert requirements
            assert all("==" in requirement for requirement in requirements), line

    def test_installs_strands_decider(self):
        assert any('"strands-decider==' in line for line in self.lines)

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
