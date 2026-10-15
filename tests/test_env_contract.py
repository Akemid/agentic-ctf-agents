"""The TypeScript fleet wiring and the Python runtimes must agree on environment variable names.

`agentcore/cdk/lib/fleet-wiring.ts` sets them; `shared/ctf_common` (and the harness agent) read
them. A rename on one side only would deploy cleanly and fail at the first match, so this test
parses both sources and compares the two sets.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIRING_TS = REPO_ROOT / "agentcore" / "cdk" / "lib" / "fleet-wiring.ts"
PYTHON_READERS = [
    *sorted((REPO_ROOT / "shared" / "ctf_common").glob("*.py")),
    REPO_ROOT / "app" / "harness" / "harness_agent.py",
]

SET_BY_TS_ONLY = {"CTF_RUNTIME_NAME"}  # identifies the runtime in logs; no Python code reads it
READ_LOCALLY_ONLY = {"CTF_MCP_URL", "CTF_DECIDER_URL"}  # local-run overrides that skip SSM; never set in AWS
NAME = r"CTF_[A-Z][A-Z0-9_]*"


def ts_names() -> set[str]:
    return set(re.findall(rf"addEnvironmentVariable\(\s*'({NAME})'", WIRING_TS.read_text()))


def python_names() -> set[str]:
    found: set[str] = set()
    for path in PYTHON_READERS:
        assert path.is_file(), path
        found |= set(re.findall(rf'^\w+\s*=\s*"({NAME})"', path.read_text(), flags=re.MULTILINE))
    return found


def test_the_parsers_find_the_variables():
    assert len(ts_names()) >= 6
    assert len(python_names()) >= 6


def test_every_variable_the_wiring_sets_is_read_by_python():
    unread = ts_names() - SET_BY_TS_ONLY - python_names()
    assert unread == set(), f"set by fleet-wiring.ts but read by no Python code: {sorted(unread)}"


def test_every_variable_python_reads_is_set_by_the_wiring():
    unset = python_names() - READ_LOCALLY_ONLY - ts_names()
    assert unset == set(), f"read by Python but never set by fleet-wiring.ts: {sorted(unset)}"


@pytest.mark.parametrize("name", sorted(SET_BY_TS_ONLY | READ_LOCALLY_ONLY))
def test_documented_exceptions_are_real(name):
    assert name in ts_names() | python_names()
