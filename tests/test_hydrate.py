"""scripts/hydrate_credentials.py (spec: fleet-operations "Secret hydration", mcp-m2m-auth
"Secrets never logged or committed"). boto3 clients are real but stubbed: no network."""

import json
import os
import stat
from pathlib import Path

import boto3
import fleet_config
import hydrate_credentials as hc
import pytest
from botocore.stub import Stubber

DISCOVERY = "https://idp.example.invalid/pool-one/.well-known/openid-configuration"
OTHER_DISCOVERY = "https://idp.example.invalid/pool-two/.well-known/openid-configuration"
SECRET_ARN = "arn:aws:secretsmanager:us-east-2:000000000000:secret:agentic-ctf/agents/{}-AbCdEf"


def client(service):
    return boto3.client(
        service, region_name="us-east-2", aws_access_key_id="test", aws_secret_access_key="test"
    )


def client_secret(member):
    return f"s3cr3t-{member.agentic_ctf_name}-VALUE"


def client_id(member):
    return f"id-{member.agentic_ctf_name}"


def ssm_names():
    names = [fleet_config.DISCOVERY_URL_PARAM]
    for item in fleet_config.FLEET:
        names.extend(fleet_config.agent_ssm_params(item))
    return names


def ssm_values(omit=(), discovery=DISCOVERY):
    values = {fleet_config.DISCOVERY_URL_PARAM: discovery}
    for item in fleet_config.FLEET:
        cid, arn = fleet_config.agent_ssm_params(item)
        values[cid] = client_id(item)
        values[arn] = SECRET_ARN.format(item.agentic_ctf_name)
    return {k: v for k, v in values.items() if k not in omit}


def stub_ssm(omit=(), discovery=DISCOVERY):
    ssm = client("ssm")
    stub = Stubber(ssm)
    names = ssm_names()
    values = ssm_values(omit, discovery)
    response = {
        "Parameters": [{"Name": n, "Value": values[n], "Type": "String"} for n in names if n in values]
    }
    invalid = [n for n in names if n not in values]
    if invalid:
        response["InvalidParameters"] = invalid
    stub.add_response("get_parameters", response, {"Names": names})
    stub.activate()
    return ssm, stub


def stub_secrets(overrides=None, errors=None):
    """One get_secret_value per member, in fleet order. `overrides` maps runtime -> SecretString;
    `errors` maps runtime -> client error code."""
    overrides, errors = overrides or {}, errors or {}
    secrets = client("secretsmanager")
    stub = Stubber(secrets)
    for item in fleet_config.FLEET:
        arn = SECRET_ARN.format(item.agentic_ctf_name)
        if item.runtime in errors:
            stub.add_client_error(
                "get_secret_value", service_error_code=errors[item.runtime], expected_params={"SecretId": arn}
            )
            continue
        body = overrides.get(
            item.runtime,
            json.dumps({"client_id": client_id(item), "client_secret": client_secret(item)}),
        )
        stub.add_response(
            "get_secret_value", {"ARN": arn, "Name": "n", "SecretString": body}, {"SecretId": arn}
        )
    stub.activate()
    return secrets, stub


@pytest.fixture
def agentcore_dir(tmp_path):
    directory = tmp_path / "agentcore"
    directory.mkdir()
    spec = {
        "name": "ctfagents",
        "runtimes": [],
        "credentials": [
            {
                "authorizerType": "OAuthCredentialProvider",
                "name": item.credential,
                "discoveryUrl": fleet_config.DISCOVERY_PLACEHOLDER,
                "scopes": ["agentic-ctf/mcp"],
                "vendor": "CustomOauth2",
                "usage": "outbound",
            }
            for item in fleet_config.FLEET
        ],
    }
    (directory / "agentcore.json").write_text(json.dumps(spec, indent=2) + "\n")
    return directory


def read_env(directory):
    pairs = {}
    for line in (directory / ".env.local").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


def discovery_urls(directory):
    spec = json.loads((directory / "agentcore.json").read_text())
    return [c["discoveryUrl"] for c in spec["credentials"]]


def run_hydrate(directory, ssm=None, secrets=None, **kwargs):
    ssm = ssm or stub_ssm()[0]
    secrets = secrets or stub_secrets()[0]
    return hc.hydrate(ssm, secrets, agentcore_dir=directory, **kwargs)


class TestHydrate:
    def test_writes_four_id_secret_pairs_with_mode_0600(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        env = read_env(agentcore_dir)
        assert len(env) == 8
        for item in fleet_config.FLEET:
            id_var, secret_var = fleet_config.credential_env_vars(item)
            assert env[id_var] == client_id(item)
            assert env[secret_var] == client_secret(item)
        assert stat.S_IMODE(os.stat(agentcore_dir / ".env.local").st_mode) == 0o600

    def test_fills_the_placeholder_discovery_url_in_every_credential(self, agentcore_dir):
        report = run_hydrate(agentcore_dir)
        assert discovery_urls(agentcore_dir) == [DISCOVERY] * 4
        assert report.discovery == "set"

    def test_rewritten_agentcore_json_keeps_everything_else(self, agentcore_dir):
        before = json.loads((agentcore_dir / "agentcore.json").read_text())
        run_hydrate(agentcore_dir)
        after = json.loads((agentcore_dir / "agentcore.json").read_text())
        for credential in after["credentials"]:
            credential["discoveryUrl"] = fleet_config.DISCOVERY_PLACEHOLDER
        assert after == before
        assert (agentcore_dir / "agentcore.json").read_text().endswith("}\n")

    def test_uses_the_single_naming_table_for_ssm_paths(self, agentcore_dir, monkeypatch):
        renamed = tuple(
            fleet_config.FleetMember(m.runtime, m.agentic_ctf_name + "-v2", m.uses_decider)
            if m.runtime == "harness_sdk"
            else m
            for m in fleet_config.FLEET
        )
        monkeypatch.setattr(fleet_config, "FLEET", renamed)
        run_hydrate(agentcore_dir)  # stubs are built from the patched table, so a wrong path would not match
        assert "AGENTCORE_CREDENTIAL_CTF_HARNESS_SDK_CLIENT_ID" in read_env(agentcore_dir)

    def test_never_prints_secrets_or_ids(self, agentcore_dir, capsys):
        ssm, secrets = stub_ssm()[0], stub_secrets()[0]
        code = hc.main(["--agentcore-dir", str(agentcore_dir)], clients=lambda region: (ssm, secrets))
        out = capsys.readouterr()
        assert code == 0
        for item in fleet_config.FLEET:
            assert client_secret(item) not in out.out + out.err
            assert client_id(item) not in out.out + out.err

    def test_second_run_is_idempotent_and_replaces_stale_values(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        first = (agentcore_dir / ".env.local").read_text()
        run_hydrate(agentcore_dir)
        assert (agentcore_dir / ".env.local").read_text() == first

    def test_keeps_unrelated_lines_in_env_local(self, agentcore_dir):
        (agentcore_dir / ".env.local").write_text(
            "OTHER_KEY=keep-me\nAGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_ID=stale\n"
        )
        run_hydrate(agentcore_dir)
        env = read_env(agentcore_dir)
        assert env["OTHER_KEY"] == "keep-me"
        assert env["AGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_ID"] == "id-harness"
        assert len(env) == 9


class TestFailures:
    def test_missing_ssm_parameter_names_the_agent_and_the_parameter_and_writes_nothing(self, agentcore_dir):
        missing = "/agentic-ctf/agents/decider-loop/client-id"
        ssm, _ = stub_ssm(omit=(missing,))
        with pytest.raises(hc.HydrateError) as caught:
            hc.hydrate(ssm, client("secretsmanager"), agentcore_dir=agentcore_dir)
        assert "decider_loop" in str(caught.value) and missing in str(caught.value)
        assert not (agentcore_dir / ".env.local").exists()
        assert discovery_urls(agentcore_dir) == [fleet_config.DISCOVERY_PLACEHOLDER] * 4

    def test_every_missing_agent_is_reported_not_just_the_first(self, agentcore_dir):
        omit = ("/agentic-ctf/agents/harness/secret-arn", "/agentic-ctf/agents/strand-decider/client-id")
        ssm, _ = stub_ssm(omit=omit)
        with pytest.raises(hc.HydrateError) as caught:
            hc.hydrate(ssm, client("secretsmanager"), agentcore_dir=agentcore_dir)
        assert "harness" in str(caught.value) and "strand_decider" in str(caught.value)

    def test_unreadable_secret_names_the_agent_and_leaves_no_partial_file(self, agentcore_dir):
        secrets, _ = stub_secrets(errors={"strand_decider": "AccessDeniedException"})
        with pytest.raises(hc.HydrateError) as caught:
            run_hydrate(agentcore_dir, secrets=secrets)
        assert "strand_decider" in str(caught.value)
        assert "AccessDeniedException" in str(caught.value)
        assert not (agentcore_dir / ".env.local").exists()
        assert list(agentcore_dir.glob(".env*")) == []  # no temp file left behind
        assert discovery_urls(agentcore_dir) == [fleet_config.DISCOVERY_PLACEHOLDER] * 4

    def test_a_failure_keeps_an_existing_env_local_untouched(self, agentcore_dir):
        (agentcore_dir / ".env.local").write_text("KEEP=1\n")
        secrets, _ = stub_secrets(errors={"harness": "ResourceNotFoundException"})
        with pytest.raises(hc.HydrateError):
            run_hydrate(agentcore_dir, secrets=secrets)
        assert (agentcore_dir / ".env.local").read_text() == "KEEP=1\n"

    def test_secret_without_client_secret_key(self, agentcore_dir):
        only_id = json.dumps({"client_id": client_id(fleet_config.member("harness_sdk"))})
        secrets, _ = stub_secrets(overrides={"harness_sdk": only_id})
        with pytest.raises(hc.HydrateError, match="harness_sdk.*client_secret"):
            run_hydrate(agentcore_dir, secrets=secrets)

    def test_secret_that_is_not_json_is_reported_without_echoing_it(self, agentcore_dir):
        secrets, _ = stub_secrets(overrides={"harness": "plain-text-leak-canary"})
        with pytest.raises(hc.HydrateError) as caught:
            run_hydrate(agentcore_dir, secrets=secrets)
        assert "harness" in str(caught.value)
        assert "plain-text-leak-canary" not in str(caught.value)

    def test_client_id_mismatch_between_ssm_and_secret(self, agentcore_dir):
        secrets, _ = stub_secrets(
            overrides={"decider_loop": json.dumps({"client_id": "someone-else", "client_secret": "x"})}
        )
        with pytest.raises(hc.HydrateError, match="decider_loop.*client_id"):
            run_hydrate(agentcore_dir, secrets=secrets)

    def test_values_that_would_break_a_dotenv_line_are_rejected_without_echoing(self, agentcore_dir):
        bad = json.dumps({"client_id": client_id(fleet_config.member("harness")), "client_secret": "a b\n#c"})
        secrets, _ = stub_secrets(overrides={"harness": bad})
        with pytest.raises(hc.HydrateError) as caught:
            run_hydrate(agentcore_dir, secrets=secrets)
        assert "harness" in str(caught.value) and "a b" not in str(caught.value)

    def test_malformed_discovery_url_in_ssm(self, agentcore_dir):
        ssm, _ = stub_ssm(discovery="http://insecure.example.invalid/x")
        with pytest.raises(hc.HydrateError, match="discovery-url"):
            run_hydrate(agentcore_dir, ssm=ssm)

    def test_credential_missing_from_agentcore_json(self, agentcore_dir):
        spec = json.loads((agentcore_dir / "agentcore.json").read_text())
        spec["credentials"].pop()
        (agentcore_dir / "agentcore.json").write_text(json.dumps(spec))
        with pytest.raises(hc.HydrateError, match="ctf_decider_loop"):
            run_hydrate(agentcore_dir)


class TestDiscoveryUrlDrift:
    def set_url(self, directory, url):
        spec = json.loads((directory / "agentcore.json").read_text())
        for credential in spec["credentials"]:
            credential["discoveryUrl"] = url
        (directory / "agentcore.json").write_text(json.dumps(spec, indent=2) + "\n")

    def test_unchanged_when_it_already_matches_ssm(self, agentcore_dir):
        self.set_url(agentcore_dir, DISCOVERY)
        assert run_hydrate(agentcore_dir).discovery == "unchanged"

    def test_drift_fails_loudly_and_changes_nothing(self, agentcore_dir):
        self.set_url(agentcore_dir, OTHER_DISCOVERY)
        with pytest.raises(hc.HydrateError, match="--update-discovery-url") as caught:
            run_hydrate(agentcore_dir)
        assert "drift" in str(caught.value).lower()
        assert discovery_urls(agentcore_dir) == [OTHER_DISCOVERY] * 4
        assert not (agentcore_dir / ".env.local").exists()

    def test_drift_is_accepted_with_the_flag(self, agentcore_dir):
        self.set_url(agentcore_dir, OTHER_DISCOVERY)
        report = run_hydrate(agentcore_dir, update_discovery_url=True)
        assert report.discovery == "updated"
        assert discovery_urls(agentcore_dir) == [DISCOVERY] * 4


class TestClean:
    def test_removes_hydrated_variables_and_restores_the_placeholder(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        hc.clean(agentcore_dir)
        assert not (agentcore_dir / ".env.local").exists()
        assert discovery_urls(agentcore_dir) == [fleet_config.DISCOVERY_PLACEHOLDER] * 4

    def test_keeps_unrelated_lines(self, agentcore_dir):
        (agentcore_dir / ".env.local").write_text("OTHER_KEY=keep-me\n")
        run_hydrate(agentcore_dir)
        hc.clean(agentcore_dir)
        assert (agentcore_dir / ".env.local").read_text() == "OTHER_KEY=keep-me\n"

    def test_is_safe_when_there_is_nothing_to_clean(self, agentcore_dir):
        hc.clean(agentcore_dir)
        assert discovery_urls(agentcore_dir) == [fleet_config.DISCOVERY_PLACEHOLDER] * 4

    def test_committed_json_returns_to_byte_identical_state(self, agentcore_dir):
        before = (agentcore_dir / "agentcore.json").read_text()
        run_hydrate(agentcore_dir)
        hc.clean(agentcore_dir)
        assert (agentcore_dir / "agentcore.json").read_text() == before


class TestCheck:
    def test_passes_after_hydrate(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        assert hc.check(agentcore_dir) == []

    def test_reports_placeholder_and_missing_variables_before_hydrate(self, agentcore_dir):
        problems = hc.check(agentcore_dir)
        assert any("placeholder" in p for p in problems)
        assert any("AGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_ID" in p for p in problems)

    def test_reports_an_empty_value(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        text = (agentcore_dir / ".env.local").read_text().replace("=id-harness\n", "=\n")
        (agentcore_dir / ".env.local").write_text(text)
        assert any("AGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_ID" in p for p in hc.check(agentcore_dir))

    def test_warns_when_the_file_is_group_readable(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        os.chmod(agentcore_dir / ".env.local", 0o644)
        assert any("0600" in p for p in hc.check(agentcore_dir))


class TestCli:
    def test_check_flag_exit_codes(self, agentcore_dir, capsys):
        assert hc.main(["--check", "--agentcore-dir", str(agentcore_dir)]) == 1
        run_hydrate(agentcore_dir)
        assert hc.main(["--check", "--agentcore-dir", str(agentcore_dir)]) == 0

    def test_clean_flag(self, agentcore_dir):
        run_hydrate(agentcore_dir)
        assert hc.main(["--clean", "--agentcore-dir", str(agentcore_dir)]) == 0
        assert not (agentcore_dir / ".env.local").exists()

    def test_error_exit_code_and_message_name_the_agent(self, agentcore_dir, capsys):
        secrets, _ = stub_secrets(errors={"harness": "AccessDeniedException"})
        ssm, _ = stub_ssm()
        code = hc.main(["--agentcore-dir", str(agentcore_dir)], clients=lambda region: (ssm, secrets))
        assert code == 1
        assert "harness" in capsys.readouterr().err


class TestRealCommittedFile:
    """The shipped agentcore.json must survive hydrate -> clean byte for byte (no skip-worktree needed)."""

    def test_round_trip_is_byte_identical(self, tmp_path):
        real = Path(__file__).resolve().parents[1] / "agentcore" / "agentcore.json"
        directory = tmp_path / "agentcore"
        directory.mkdir()
        (directory / "agentcore.json").write_text(real.read_text())
        run_hydrate(directory)
        assert fleet_config.DISCOVERY_PLACEHOLDER not in (directory / "agentcore.json").read_text()
        hc.clean(directory)
        assert (directory / "agentcore.json").read_text() == real.read_text()


def fail_env_write(monkeypatch):
    """Make the .env.local write fail (after agentcore.json was written). Returns the real writer."""
    real = hc._atomic_write

    def flaky(path, text, mode):
        if path.name == ".env.local":
            raise OSError("disk full")
        real(path, text, mode)

    monkeypatch.setattr(hc, "_atomic_write", flaky)
    return real


class TestPairedWrites:
    """agentcore.json and .env.local are written as a pair: a failure in between must be detectable."""

    def test_agentcore_json_is_written_first_and_env_local_second(self, agentcore_dir, monkeypatch):
        order = []
        real = hc._atomic_write

        def recording(path, text, mode):
            order.append(path.name)
            real(path, text, mode)

        monkeypatch.setattr(hc, "_atomic_write", recording)
        run_hydrate(agentcore_dir)
        assert order == ["agentcore.json", ".env.local"]

    def test_a_failure_between_the_two_writes_is_caught_by_check(self, agentcore_dir, monkeypatch):
        real = fail_env_write(monkeypatch)
        with pytest.raises(OSError, match="disk full"):
            run_hydrate(agentcore_dir)
        monkeypatch.setattr(hc, "_atomic_write", real)
        assert not (agentcore_dir / ".env.local").exists()
        assert any(".env.local does not exist" in p for p in hc.check(agentcore_dir))
        assert hc.main(["--check", "--agentcore-dir", str(agentcore_dir)]) == 1

    def test_clean_after_a_half_finished_hydrate_restores_the_placeholder(self, agentcore_dir, monkeypatch):
        real = fail_env_write(monkeypatch)
        with pytest.raises(OSError):
            run_hydrate(agentcore_dir)
        monkeypatch.setattr(hc, "_atomic_write", real)
        hc.clean(agentcore_dir)
        assert set(discovery_urls(agentcore_dir)) == {fleet_config.DISCOVERY_PLACEHOLDER}


class TestFileModes:
    @pytest.mark.parametrize("mode", [0o600, 0o640, 0o664, 0o644])
    def test_agentcore_json_keeps_its_original_mode_through_hydrate_and_clean(self, agentcore_dir, mode):
        spec = agentcore_dir / "agentcore.json"
        spec.chmod(mode)
        run_hydrate(agentcore_dir)
        assert stat.S_IMODE(spec.stat().st_mode) == mode
        hc.clean(agentcore_dir)
        assert stat.S_IMODE(spec.stat().st_mode) == mode

    def test_env_local_is_still_0600(self, agentcore_dir):
        (agentcore_dir / "agentcore.json").chmod(0o664)
        run_hydrate(agentcore_dir)
        assert stat.S_IMODE((agentcore_dir / ".env.local").stat().st_mode) == 0o600
