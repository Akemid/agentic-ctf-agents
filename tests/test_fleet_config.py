"""The committed fleet definition: agentcore.json, the single naming table, and public-repo hygiene
(spec: ctf-agent-runtimes, mcp-m2m-auth "Per-runtime credential provider", fleet-operations)."""

import json
import re
from pathlib import Path

import fleet_config
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTCORE_JSON = REPO_ROOT / "agentcore" / "agentcore.json"
RUNTIME_NAMES = ["harness", "harness_sdk", "strand_decider", "decider_loop"]


@pytest.fixture(scope="module")
def spec():
    return json.loads(AGENTCORE_JSON.read_text())


class TestNamingTable:
    def test_members_are_the_four_runtimes_in_order(self):
        assert [m.runtime for m in fleet_config.FLEET] == RUNTIME_NAMES

    def test_agentic_ctf_names_are_hyphen_only_and_match_its_name_rule(self):
        names = {m.runtime: m.agentic_ctf_name for m in fleet_config.FLEET}
        assert names == {
            "harness": "harness",
            "harness_sdk": "harness-sdk",
            "strand_decider": "strand-decider",
            "decider_loop": "decider-loop",
        }
        rule = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")  # agentic-ctf cognito-auth naming
        assert all(rule.fullmatch(n) for n in names.values())

    def test_only_the_decider_runtimes_use_the_decider(self):
        assert {m.runtime for m in fleet_config.FLEET if m.uses_decider} == {"strand_decider", "decider_loop"}

    def test_credential_names_are_distinct_and_avoid_reserved_cli_suffixes(self):
        credentials = [m.credential for m in fleet_config.FLEET]
        assert len(set(credentials)) == 4
        for name in credentials:
            assert name.replace("-", "_").upper().endswith(("_API_KEY_ID", "_APP_ID", "_CLIENT_ID")) is False

    def test_env_var_names_follow_the_cli_normalisation_without_a_project_prefix(self):
        member = fleet_config.member("harness_sdk")
        assert fleet_config.credential_env_vars(member) == (
            "AGENTCORE_CREDENTIAL_CTF_HARNESS_SDK_CLIENT_ID",
            "AGENTCORE_CREDENTIAL_CTF_HARNESS_SDK_CLIENT_SECRET",
        )

    def test_unknown_runtime_is_rejected_by_name(self):
        with pytest.raises(KeyError, match="nope"):
            fleet_config.member("nope")


class TestAgentcoreJson:
    def test_declares_exactly_the_four_runtimes(self, spec):
        assert [r["name"] for r in spec["runtimes"]] == RUNTIME_NAMES

    @pytest.mark.parametrize("name", RUNTIME_NAMES)
    def test_runtime_shape(self, spec, name):
        runtime = next(r for r in spec["runtimes"] if r["name"] == name)
        assert runtime == {
            "name": name,
            "build": "CodeZip",
            "entrypoint": "main.py",
            "codeLocation": f"app/{name}/",
            "runtimeVersion": "PYTHON_3_14",
            "networkMode": "PUBLIC",
            "protocol": "HTTP",
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 14400},
        }
        assert (REPO_ROOT / runtime["codeLocation"] / runtime["entrypoint"]).is_file()

    def test_each_runtime_has_its_own_outbound_oauth_credential(self, spec):
        by_name = {c["name"]: c for c in spec["credentials"]}
        assert list(by_name) == [m.credential for m in fleet_config.FLEET]
        for credential in by_name.values():
            assert credential["authorizerType"] == "OAuthCredentialProvider"
            assert credential["vendor"] == "CustomOauth2"
            assert credential["usage"] == "outbound"
            assert credential["scopes"] == ["agentic-ctf/mcp"]

    def test_max_lifetime_constant_equals_every_runtime_lifecycle(self, spec):
        lifetimes = {r["lifecycleConfiguration"]["maxLifetime"] for r in spec["runtimes"]}
        assert lifetimes == {fleet_config.MAX_LIFETIME_SECONDS}

    def test_committed_discovery_url_is_the_placeholder(self, spec):
        urls = {c["discoveryUrl"] for c in spec["credentials"]}
        assert urls == {fleet_config.DISCOVERY_PLACEHOLDER}
        assert fleet_config.DISCOVERY_PLACEHOLDER.endswith("/.well-known/openid-configuration")
        assert fleet_config.is_placeholder(fleet_config.DISCOVERY_PLACEHOLDER) is True
        assert (
            fleet_config.is_placeholder(
                "https://cognito-idp.example.invalid/x/.well-known/openid-configuration"
            )
            is False
        )


def _public_files() -> list[str]:
    """Every committed file this slice owns that could leak account data, found by glob so that a commit
    that has not landed yet simply contributes nothing (no hard-coded list to keep in step)."""
    patterns = [
        "agentcore/agentcore.json",
        "agentcore/cdk/lib/*.ts",
        "agentcore/cdk/test/*.ts",
        "scripts/*.py",
        "scripts/*.sh",
        "docs/*.md",
        "Makefile",
    ]
    found = {path for pattern in patterns for path in REPO_ROOT.glob(pattern) if path.is_file()}
    return sorted(str(path.relative_to(REPO_ROOT)) for path in found)


PUBLIC_FILES = _public_files()
FORBIDDEN = {
    "an AWS account id": r"\b\d{12}\b",
    "a Cognito user pool id": r"\b[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{6,}\b",
    "an email address": r"[\w.+-]+@(?!example\.invalid)[\w-]+\.[a-z]{2,}",
    "a home directory path": r"/Users/|/home/\w+",
    "an access key id": r"\bAKIA[0-9A-Z]{12,}\b",
}


class TestPublicRepoHygiene:
    def test_the_scan_covers_files(self):
        assert PUBLIC_FILES, "the glob patterns matched nothing: the hygiene scan would pass vacuously"
        assert "agentcore/agentcore.json" in PUBLIC_FILES

    @pytest.mark.parametrize("relative", PUBLIC_FILES)
    def test_file_is_free_of_account_specific_values(self, relative):
        path = REPO_ROOT / relative
        text = path.read_text()
        found = [
            (what, m.group(0))
            for what, pattern in FORBIDDEN.items()
            for m in re.finditer(pattern, text)
            if m.group(0) != "000000000000"  # the all-zero placeholder account used by fixtures
        ]
        assert found == []

    def test_env_local_is_gitignored_at_both_levels(self):
        for gitignore in (REPO_ROOT / ".gitignore", REPO_ROOT / "agentcore" / ".gitignore"):
            assert ".env.local" in gitignore.read_text().split()
        assert ".fleet/" in (REPO_ROOT / ".gitignore").read_text().split()
