"""The safe deploy path: scripts/deploy_fleet.sh (and `make deploy`, which calls it).

`agentcore deploy` silently SKIPS an OAuth provider whose credentials are missing (it only logs
`Skipped <provider>`), so a deploy without hydration "succeeds" with no providers. The wrapper makes the
right order the only order and always cleans the secrets up afterwards. These tests run the real script
with fake `uv` and `agentcore` executables first on PATH: no AWS, no network, no real hydration.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_fleet.sh"

SYNC = "uv run python scripts/sync_common.py --check"
HYDRATE = "uv run python scripts/hydrate_credentials.py"
CHECK = "uv run python scripts/hydrate_credentials.py --check"
CLEAN = "uv run python scripts/hydrate_credentials.py --clean"
VALIDATE = "agentcore validate"
DEPLOY = "agentcore deploy --yes"
HAPPY = [SYNC, HYDRATE, CHECK, VALIDATE, DEPLOY, CLEAN]

FAKE = """#!/bin/sh
# Records the call; fails (with FAKE_FAIL_CODE) when the call contains FAKE_FAIL_ON,
# or when it is exactly FAKE_FAIL_EXACT.
echo "$(basename "$0") $*" >> "$FAKE_LOG"
if [ "$(basename "$0")" = agentcore ] && [ "$1" = deploy ] && [ -n "${FAKE_DEPLOY_MARKER:-}" ]; then
  : > "$FAKE_DEPLOY_MARKER"
  sleep 20
fi
if [ "$(basename "$0") $*" = "${FAKE_FAIL_EXACT:-@@never@@}" ]; then exit "${FAKE_FAIL_CODE:-3}"; fi
case "$(basename "$0") $*" in
  *"${FAKE_FAIL_ON:-@@never@@}"*) exit "${FAKE_FAIL_CODE:-3}" ;;
esac
exit 0
"""


@pytest.fixture
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uv", "agentcore"):
        fake = bin_dir / name
        fake.write_text(FAKE)
        fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
    }
    return env, log


def calls(log):
    return [line for line in log.read_text().splitlines() if line]


def run_script(rig, *, yes=True, fail_on=None, fail_code=3):
    env, log = rig
    env = dict(env)
    if yes:
        env["YES"] = "1"
    if fail_on:
        env["FAKE_FAIL_ON"] = fail_on
        env["FAKE_FAIL_CODE"] = str(fail_code)
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return result, calls(log)


def test_the_script_exists_is_executable_and_is_bash_strict():
    text = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_without_yes_it_prints_the_plan_and_runs_nothing(rig):
    result, log = run_script(rig, yes=False)
    assert result.returncode == 0 and log == []
    assert "YES=1" in result.stdout
    for step in ("sync_common", "hydrate_credentials", "agentcore validate", "agentcore deploy"):
        assert step in result.stdout


def test_yes_must_be_exactly_one(rig):
    env, log = rig
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)], env={**env, "YES": "true"}, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and calls(log) == []


def test_happy_path_runs_the_steps_in_order_and_cleans_last(rig):
    result, log = run_script(rig)
    assert result.returncode == 0, result.stderr
    assert log == HAPPY


@pytest.mark.parametrize(
    ("fail_on", "ran"),
    [
        (SYNC, [SYNC]),
        (CHECK, [SYNC, HYDRATE, CHECK]),
        ("agentcore validate", [SYNC, HYDRATE, CHECK, VALIDATE]),
        ("agentcore deploy", [SYNC, HYDRATE, CHECK, VALIDATE, DEPLOY]),
    ],
)
def test_a_failing_step_stops_the_run_but_cleanup_still_runs(rig, fail_on, ran):
    result, log = run_script(rig, fail_on=fail_on, fail_code=7)
    assert log == [*ran, CLEAN]
    assert result.returncode == 7  # the failing step's code, not the cleanup's


def test_a_failing_hydrate_never_reaches_check_validate_or_deploy_but_still_cleans(rig):
    env, log = rig
    # `--check` and `--clean` contain the plain hydrate command line, so match the exact call.
    env = {**env, "YES": "1", "FAKE_FAIL_EXACT": HYDRATE, "FAKE_FAIL_CODE": "4"}
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
    assert calls(log) == [SYNC, HYDRATE, CLEAN]
    assert result.returncode == 4


def test_update_discovery_url_is_passed_to_the_hydrate_step_only_when_asked(rig):
    env, log = rig
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={**env, "YES": "1", "UPDATE_DISCOVERY_URL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert calls(log) == [SYNC, f"{HYDRATE} --update-discovery-url", CHECK, VALIDATE, DEPLOY, CLEAN]


def test_a_failing_cleanup_makes_a_successful_run_fail_loudly(rig):
    result, log = run_script(rig, fail_on="--clean", fail_code=9)
    assert log == HAPPY
    assert result.returncode != 0
    assert "clean" in result.stderr.lower()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupt_during_deploy_still_cleans(rig, tmp_path, sig):
    env, log = rig
    marker = tmp_path / "deploy-started"
    process = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env={**env, "YES": "1", "FAKE_DEPLOY_MARKER": str(marker)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the fake deploy never started"
        os.killpg(process.pid, sig)  # the whole group, like Ctrl-C in a terminal
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    assert calls(log)[-1] == CLEAN
    assert process.returncode != 0


class TestMakefile:
    makefile = REPO_ROOT / "Makefile"

    def test_deploy_target_calls_the_wrapper(self):
        assert self.makefile.is_file()
        make = shutil.which("make")
        assert make
        out = subprocess.run(
            [make, "-n", "-C", str(REPO_ROOT), "deploy", "YES=1"], capture_output=True, text=True, check=True
        ).stdout
        assert "scripts/deploy_fleet.sh" in out
        assert "agentcore deploy" not in out  # the Makefile never deploys by itself

    def test_make_deploy_without_yes_is_a_dry_run(self, rig):
        env, log = rig
        result = subprocess.run(
            ["make", "-C", str(REPO_ROOT), "deploy"], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0 and calls(log) == []
        assert "YES=1" in result.stdout

    def test_make_deploy_with_yes_runs_the_wrapper_order(self, rig):
        env, log = rig
        command = ["make", "-C", str(REPO_ROOT), "deploy", "YES=1"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert calls(log) == HAPPY
