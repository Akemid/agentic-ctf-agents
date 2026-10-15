"""scripts/fleet.py: start / status / stop (spec: fleet-operations "Fleet launcher" and
"Status and stop"). The bedrock-agentcore client is real but stubbed: no network."""

import io
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import fleet
import fleet_config
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from ctf_common.payload import Action, parse_payload

REGION = "us-east-2"
ARN = "arn:aws:bedrock-agentcore:" + REGION + ":000000000000:runtime/ctfagents_{}-AbCdEfGhIj"
RUNTIMES = [m.runtime for m in fleet_config.FLEET]
SESSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{32,255}$")


class Capture:
    """Stubber expected-param that accepts anything and remembers what it saw."""

    def __init__(self):
        self.values = []

    def __eq__(self, other):
        self.values.append(other)
        return True

    __hash__ = None


def body(obj):
    raw = json.dumps(obj).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def make_client():
    client = boto3.client(
        "bedrock-agentcore", region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )
    return client, Stubber(client)


class Clock:
    """An injectable UTC clock the tests move by hand."""

    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Rig:
    """A stubbed client plus capture of every invocation, in call order."""

    def __init__(self):
        self.client, self.stub = make_client()
        self.calls = []
        self.clock = Clock()
        self.invoked = 0  # InvokeAgentRuntime calls that reached the client, stubbed or not
        event = "before-parameter-build.bedrock-agentcore.InvokeAgentRuntime"
        self.client.meta.events.register(event, self._count)

    def _count(self, **_kwargs):
        self.invoked += 1

    def invoke(self, runtime, reply=None, error=None):
        expected = {
            "agentRuntimeArn": ARN.format(runtime),
            "runtimeSessionId": Capture(),
            "payload": Capture(),
            "runtimeUserId": Capture(),
            "contentType": "application/json",
            "accept": "application/json",
        }
        if error:
            self.stub.add_client_error(
                "invoke_agent_runtime", service_error_code=error, expected_params=expected
            )
        else:
            self.stub.add_response(
                "invoke_agent_runtime",
                {
                    "response": body(reply or {"status": "started"}),
                    "statusCode": 200,
                    "contentType": "application/json",
                },
                expected,
            )
        self.calls.append(expected)
        return expected

    def stop_session(self, runtime):
        expected = {"agentRuntimeArn": ARN.format(runtime), "runtimeSessionId": Capture()}
        self.stub.add_response("stop_runtime_session", {"statusCode": 200}, expected)
        self.calls.append(expected)
        return expected

    def stop_session_error(self, runtime, code):
        expected = {"agentRuntimeArn": ARN.format(runtime), "runtimeSessionId": Capture()}
        self.stub.add_client_error(
            "stop_runtime_session", service_error_code=code, http_status_code=404, expected_params=expected
        )
        self.calls.append(expected)
        return expected

    def factory(self, region):
        assert region == REGION  # derived from the runtime ARN
        return self.client


@pytest.fixture
def rig():
    rig = Rig()
    rig.stub.activate()  # activated once; any call without a queued response raises
    yield rig
    rig.stub.assert_no_pending_responses()


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "fleet-state"


@pytest.fixture
def arn_args():
    return [arg for rt in RUNTIMES for arg in ("--runtime-arn", f"{rt}={ARN.format(rt)}")]


def run(argv, rig, state_dir, capsys=None):
    code = fleet.main(["--state-dir", str(state_dir), *argv], client_factory=rig.factory, clock=rig.clock)
    out = capsys.readouterr() if capsys else None
    return code, out


def payloads(expected):
    return [json.loads(c["payload"].values[0]) for c in expected if isinstance(c["payload"], Capture)]


def full_launch_args(arn_args, per_type=2, match="m1"):
    agents = [arg for rt in RUNTIMES for arg in ("--agent", f"{rt}={per_type}")]
    return ["start", "--match", match, *agents, *arn_args]


def stub_full_launch(rig, per_type=2, replies=None):
    expected = []
    for rt in RUNTIMES:
        for _ in range(per_type):
            expected.append(rig.invoke(rt, reply=(replies or {}).get(rt)))
    return expected


class TestPlanning:
    def test_expands_counts_into_named_characters_in_fleet_order(self):
        plan = fleet.plan_launch("m1", {"decider_loop": 2, "harness": 1}, teams=2)
        assert [(c.runtime, c.name) for c in plan] == [
            ("harness", "harness-1"),
            ("decider_loop", "decider-loop-1"),
            ("decider_loop", "decider-loop-2"),
        ]

    def test_two_teams_alternate_and_one_team_is_all_red(self):
        two = fleet.plan_launch("m1", {"harness": 2, "harness_sdk": 1}, teams=2)
        assert [c.team for c in two] == ["red", "blue", "red"]
        one = fleet.plan_launch("m1", {"harness": 2, "harness_sdk": 1}, teams=1)
        assert {c.team for c in one} == {"red"}

    def test_session_ids_are_distinct_valid_and_at_least_33_chars(self):
        plan = fleet.plan_launch("m_1-x", {rt: 3 for rt in RUNTIMES}, teams=2)
        ids = [c.session_id for c in plan]
        assert len(plan) == 12
        assert len(set(ids)) == 12
        assert all(SESSION_RE.fullmatch(i) for i in ids)
        assert all(len(i) >= 33 for i in ids)

    def test_short_match_ids_still_give_long_session_ids(self):
        (only,) = fleet.plan_launch("a", {"harness": 1}, teams=1)
        assert len(only.session_id) >= 33

    @pytest.mark.parametrize("count", [4, 10])
    def test_more_than_three_of_one_type_is_rejected_naming_the_type(self, count):
        with pytest.raises(
            fleet.LaunchError, match=rf"strand_decider.*{fleet_config.MAX_CHARACTERS_PER_TYPE}"
        ):
            fleet.plan_launch("m1", {"harness": 1, "strand_decider": count}, teams=2)

    def test_exactly_three_is_allowed(self):
        assert len(fleet.plan_launch("m1", {"strand_decider": 3}, teams=2)) == 3

    def test_more_than_two_teams_rejected(self):
        with pytest.raises(fleet.LaunchError, match="at most 2 teams"):
            fleet.plan_launch("m1", {"harness": 1}, teams=3)
        with pytest.raises(fleet.LaunchError, match="at least 1"):
            fleet.plan_launch("m1", {"harness": 1}, teams=0)

    def test_unknown_runtime_empty_request_and_zero_counts_are_rejected(self):
        with pytest.raises(fleet.LaunchError, match="unknown runtime: nope"):
            fleet.plan_launch("m1", {"nope": 1}, teams=2)
        with pytest.raises(fleet.LaunchError, match="no characters"):
            fleet.plan_launch("m1", {}, teams=2)
        with pytest.raises(fleet.LaunchError, match="no characters"):
            fleet.plan_launch("m1", {"harness": 0}, teams=2)
        with pytest.raises(fleet.LaunchError, match="negative"):
            fleet.plan_launch("m1", {"harness": -1}, teams=2)

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "a/b"])
    def test_invalid_match_id_is_rejected(self, bad):
        with pytest.raises(fleet.LaunchError, match="match id"):
            fleet.plan_launch(bad, {"harness": 1}, teams=2)

    @pytest.mark.parametrize("runtime", RUNTIMES)
    def test_payload_is_accepted_by_the_runtime_parser(self, runtime):
        (character,) = fleet.plan_launch("m1", {runtime: 1}, teams=2)
        payload = fleet.build_payload(character, model="opus")
        request = parse_payload(payload, runtime=runtime, session_id=character.session_id)
        assert request.action is Action.START
        assert (request.match_id, request.name, request.team, request.model) == (
            "m1",
            character.name,
            character.team,
            "opus",
        )


class TestStart:
    def test_dry_run_is_the_default_and_invokes_nothing(self, rig, state_dir, arn_args, capsys):
        code, out = run(
            full_launch_args(arn_args), rig, state_dir, capsys
        )  # nothing stubbed: a call would raise
        assert code == 0
        assert "dry run" in out.out.lower()
        assert out.out.count("decider-loop-") == 2 and "--yes" in out.out
        assert not state_dir.exists()

    def test_launch_makes_eight_invocations_with_eight_distinct_session_ids(self, rig, state_dir, arn_args):
        expected = stub_full_launch(rig)
        code, _ = run([*full_launch_args(arn_args), "--yes"], rig, state_dir)
        assert code == 0
        ids = [c["runtimeSessionId"].values[0] for c in expected]
        assert len(ids) == 8 and len(set(ids)) == 8
        assert all(len(i) >= 33 for i in ids)

    def test_passes_a_runtime_user_id_and_a_start_payload(self, rig, state_dir, arn_args):
        expected = stub_full_launch(rig, per_type=1)
        run([*full_launch_args(arn_args, per_type=1), "--yes", "--model", "nova-lite"], rig, state_dir)
        users = {c["runtimeUserId"].values[0] for c in expected}
        assert len(users) == 1 and users != {""}
        sent = payloads(expected)
        assert [p["action"] for p in sent] == ["start"] * 4
        assert {p["model"] for p in sent} == {"nova-lite"}
        assert {p["match_id"] for p in sent} == {"m1"}

    def test_records_every_session_in_the_state_file(self, rig, state_dir, arn_args):
        expected = stub_full_launch(rig)
        run([*full_launch_args(arn_args), "--yes"], rig, state_dir)
        saved = json.loads((state_dir / "m1.json").read_text())
        assert saved["match_id"] == "m1"
        assert [s["session_id"] for s in saved["sessions"]] == [
            c["runtimeSessionId"].values[0] for c in expected
        ]
        assert {s["runtime"] for s in saved["sessions"]} == set(RUNTIMES)
        assert {s["ack"] for s in saved["sessions"]} == {"started"}

    def test_limit_exceeded_rejects_before_any_invocation(self, rig, state_dir, arn_args, capsys):
        code, out = run([*full_launch_args(arn_args, per_type=4), "--yes"], rig, state_dir, capsys)
        assert code == 2
        assert "at most 3" in out.err
        assert not state_dir.exists()

    def test_a_failed_invocation_does_not_stop_the_others_and_exits_non_zero(
        self, rig, state_dir, arn_args, capsys
    ):
        rig.invoke("harness")
        rig.invoke("harness", error="ThrottlingException")
        rig.invoke("harness_sdk")
        rig.invoke("harness_sdk")
        args = [
            "start",
            "--match",
            "m1",
            "--agent",
            "harness=2",
            "--agent",
            "harness_sdk=2",
            *arn_args,
            "--yes",
        ]
        code, out = run(args, rig, state_dir, capsys)
        assert code == 1
        assert "harness-2" in out.err and "ThrottlingException" in out.err
        saved = json.loads((state_dir / "m1.json").read_text())
        assert [s["ack"] for s in saved["sessions"]] == ["started", "failed", "started", "started"]

    def test_a_runtime_that_rejects_the_start_counts_as_a_failure(self, rig, state_dir, arn_args, capsys):
        rig.invoke("harness", reply={"status": "rejected", "error": "preflight: SSM parameter not found"})
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir, capsys
        )
        assert code == 1
        assert "harness-1" in out.err and "SSM parameter not found" in out.err

    def test_already_running_is_an_acknowledgement(self, rig, state_dir, arn_args):
        rig.invoke("harness", reply={"status": "already_running"})
        code, _ = run(["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir)
        assert code == 0

    def test_non_json_runtime_reply_is_a_failure_without_dumping_it(self, rig, state_dir, arn_args, capsys):
        rig.stub.add_response(
            "invoke_agent_runtime",
            {
                "response": StreamingBody(io.BytesIO(b"<html>oops</html>"), 17),
                "statusCode": 200,
                "contentType": "text/html",
            },
            {
                "agentRuntimeArn": ARN.format("harness"),
                "runtimeSessionId": Capture(),
                "payload": Capture(),
                "runtimeUserId": Capture(),
                "contentType": "application/json",
                "accept": "application/json",
            },
        )
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir, capsys
        )
        assert code == 1 and "not JSON" in out.err

    def test_failed_characters_do_not_count_against_the_cap_and_keep_their_names_reserved(
        self, rig, state_dir, arn_args
    ):
        rig.invoke("harness", error="ThrottlingException")
        run(["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir)
        for _ in range(3):
            rig.invoke("harness")
        code, _ = run(["start", "--match", "m1", "--agent", "harness=3", *arn_args, "--yes"], rig, state_dir)
        assert code == 0
        saved = json.loads((state_dir / "m1.json").read_text())
        assert [s["name"] for s in saved["sessions"]] == ["harness-1", "harness-2", "harness-3", "harness-4"]

    def test_a_second_launch_for_the_same_match_continues_the_numbering(self, rig, state_dir, arn_args):
        rig.invoke("harness")
        rig.invoke("harness_sdk")
        run(["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir)
        run(["start", "--match", "m1", "--agent", "harness_sdk=1", *arn_args, "--yes"], rig, state_dir)
        saved = json.loads((state_dir / "m1.json").read_text())
        assert [s["name"] for s in saved["sessions"]] == ["harness-1", "harness-sdk-1"]

    def test_the_per_type_cap_counts_characters_already_recorded_for_the_match(
        self, rig, state_dir, arn_args, capsys
    ):
        for _ in range(3):
            rig.invoke("harness")
        run(["start", "--match", "m1", "--agent", "harness=3", *arn_args, "--yes"], rig, state_dir)
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir, capsys
        )
        assert code == 2 and "at most 3" in out.err


class TestRuntimeArns:
    def write_deployed_state(self, directory, runtimes=RUNTIMES, targets=("default",)):
        directory.mkdir(parents=True)
        state = {
            "targets": {
                t: {
                    "resources": {
                        "runtimes": {
                            rt: {"runtimeId": rt, "runtimeArn": ARN.format(rt), "runtimeEndpoint": "e"}
                            for rt in runtimes
                        }
                    }
                }
                for t in targets
            }
        }
        (directory / "deployed-state.json").write_text(json.dumps(state))
        return directory / "deployed-state.json"

    def test_arns_come_from_deployed_state_when_not_given(self, rig, state_dir, tmp_path):
        path = self.write_deployed_state(tmp_path / ".cli")
        rig.invoke("harness")
        code, _ = run(
            ["start", "--match", "m1", "--agent", "harness=1", "--deployed-state", str(path), "--yes"],
            rig,
            state_dir,
        )
        assert code == 0

    def test_a_missing_runtime_arn_is_rejected_by_name_before_any_invocation(
        self, rig, state_dir, tmp_path, capsys
    ):
        path = self.write_deployed_state(tmp_path / ".cli", runtimes=["harness"])
        code, out = run(
            ["start", "--match", "m1", "--agent", "decider_loop=1", "--deployed-state", str(path), "--yes"],
            rig,
            state_dir,
            capsys,
        )
        assert code == 2 and "decider_loop" in out.err and "deployed-state" in out.err

    def test_absent_deployed_state_file_explains_how_to_proceed(self, rig, state_dir, tmp_path, capsys):
        code, out = run(
            [
                "start",
                "--match",
                "m1",
                "--agent",
                "harness=1",
                "--deployed-state",
                str(tmp_path / "missing.json"),
                "--yes",
            ],
            rig,
            state_dir,
            capsys,
        )
        assert code == 2 and "--runtime-arn" in out.err

    def test_several_targets_require_an_explicit_one(self, rig, state_dir, tmp_path, capsys):
        path = self.write_deployed_state(tmp_path / ".cli", targets=("default", "other"))
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", "--deployed-state", str(path), "--yes"],
            rig,
            state_dir,
            capsys,
        )
        assert code == 2 and "--target" in out.err
        rig.invoke("harness")
        code, _ = run(
            [
                "start",
                "--match",
                "m1",
                "--agent",
                "harness=1",
                "--deployed-state",
                str(path),
                "--target",
                "other",
                "--yes",
            ],
            rig,
            state_dir,
        )
        assert code == 0


def launched(rig, state_dir, arn_args, replies=None):
    expected = stub_full_launch(rig, replies=replies)
    assert run([*full_launch_args(arn_args), "--yes"], rig, state_dir)[0] == 0
    return [c["runtimeSessionId"].values[0] for c in expected]


class TestStatus:
    def test_sends_status_to_every_recorded_session_with_the_same_ids(self, rig, state_dir, arn_args, capsys):
        ids = launched(rig, state_dir, arn_args)
        replies = {rt: {"status": "running", "ticks": 7, "name": "x"} for rt in RUNTIMES}
        status_calls = [rig.invoke(rt, reply=replies[rt]) for rt in RUNTIMES for _ in range(2)]
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0
        assert [c["runtimeSessionId"].values[-1] for c in status_calls] == ids
        assert all(p == {"action": "status"} for p in payloads(status_calls))
        assert out.out.count("running") == 8 and "7" in out.out

    def test_a_failing_session_is_reported_and_the_exit_code_is_non_zero(
        self, rig, state_dir, arn_args, capsys
    ):
        launched(rig, state_dir, arn_args)
        for rt in RUNTIMES:
            for i in range(2):
                if (rt, i) == ("decider_loop", 1):
                    rig.invoke(rt, error="ThrottlingException")
                else:
                    rig.invoke(rt, reply={"status": "finished", "ticks": 3})
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 1
        assert out.out.count("finished") == 7
        assert "decider-loop-2" in out.err

    def test_without_a_match_it_reports_every_recorded_match(self, rig, state_dir, arn_args, capsys):
        rig.invoke("harness")
        rig.invoke("harness_sdk")
        run(["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir)
        run(["start", "--match", "m2", "--agent", "harness_sdk=1", *arn_args, "--yes"], rig, state_dir)
        rig.invoke("harness", reply={"status": "running"})
        rig.invoke("harness_sdk", reply={"status": "idle"})
        code, out = run(["status"], rig, state_dir, capsys)
        assert code == 0 and "m1" in out.out and "m2" in out.out

    def test_unknown_match_or_empty_state_is_a_clear_error(self, rig, state_dir, capsys):
        code, out = run(["status", "--match", "ghost"], rig, state_dir, capsys)
        assert code == 2 and "ghost" in out.err


class TestStop:
    def test_stop_without_a_selector_is_a_usage_error(self, rig, state_dir, capsys):
        code, out = run(["stop", "--yes"], rig, state_dir, capsys)
        assert code == 2 and "--match" in out.err and "--all" in out.err

    def test_dry_run_is_the_default(self, rig, state_dir, arn_args, capsys):
        launched(rig, state_dir, arn_args)
        code, out = run(["stop", "--match", "m1"], rig, state_dir, capsys)  # nothing more stubbed
        assert code == 0 and "dry run" in out.out.lower() and "--yes" in out.out

    def test_stop_all_sends_stop_to_each_of_the_eight_session_ids(self, rig, state_dir, arn_args):
        ids = launched(rig, state_dir, arn_args)
        calls = [rig.invoke(rt, reply={"status": "stopping"}) for rt in RUNTIMES for _ in range(2)]
        code, _ = run(["stop", "--all", "--yes"], rig, state_dir)
        assert code == 0
        assert [c["runtimeSessionId"].values[-1] for c in calls] == ids
        assert all(p == {"action": "stop"} for p in payloads(calls))

    def test_hard_stop_uses_stop_runtime_session_with_the_same_ids(self, rig, state_dir, arn_args):
        ids = launched(rig, state_dir, arn_args)
        calls = [rig.stop_session(rt) for rt in RUNTIMES for _ in range(2)]
        code, _ = run(["stop", "--match", "m1", "--hard", "--yes"], rig, state_dir)
        assert code == 0
        assert [c["runtimeSessionId"].values[0] for c in calls] == ids

    def test_stop_failure_on_one_session_still_stops_the_rest(self, rig, state_dir, arn_args, capsys):
        launched(rig, state_dir, arn_args)
        for rt in RUNTIMES:
            for i in range(2):
                if (rt, i) == ("harness", 0):
                    rig.invoke(rt, error="ThrottlingException")
                else:
                    rig.invoke(rt, reply={"status": "stopping"})
        code, out = run(["stop", "--match", "m1", "--yes"], rig, state_dir, capsys)
        assert code == 1 and "harness-1" in out.err

    def test_a_rejected_stop_reply_is_a_failure(self, rig, state_dir, arn_args, capsys):
        rig.invoke("harness")
        run(["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir)
        rig.invoke("harness", reply={"status": "rejected", "error": "nothing to stop"})
        code, out = run(["stop", "--match", "m1", "--yes"], rig, state_dir, capsys)
        assert code == 1 and "nothing to stop" in out.err

    def test_stop_marks_sessions_in_the_state_file(self, rig, state_dir, arn_args):
        launched(rig, state_dir, arn_args)
        for rt in RUNTIMES:
            for _ in range(2):
                rig.invoke(rt, reply={"status": "stopping"})
        run(["stop", "--match", "m1", "--yes"], rig, state_dir)
        saved = json.loads((state_dir / "m1.json").read_text())
        assert {s["stop"] for s in saved["sessions"]} == {"requested"}
        assert len(saved["sessions"]) == 8  # kept, so a later --hard still has the ids


class TestHygiene:
    def test_state_dir_defaults_to_the_gitignored_fleet_directory(self):
        assert fleet.DEFAULT_STATE_DIR == Path(fleet.__file__).resolve().parents[1] / ".fleet"

    def test_state_files_hold_no_secrets_only_ids_arns_and_acks(self, rig, state_dir, arn_args):
        stub_full_launch(rig, per_type=1)
        run([*full_launch_args(arn_args, per_type=1), "--yes"], rig, state_dir)
        saved = json.loads((state_dir / "m1.json").read_text())
        keys = {k for s in saved["sessions"] for k in s}
        assert keys <= {
            "runtime",
            "name",
            "team",
            "session_id",
            "runtime_arn",
            "ack",
            "error",
            "stop",
            "started_at",
            "last_seen",
            "last_status",
        }


def start_one(rig, state_dir, arn_args, runtime="harness", match="m1", count=1):
    for _ in range(count):
        rig.invoke(runtime)
    args = ["start", "--match", match, "--agent", f"{runtime}={count}", *arn_args, "--yes"]
    assert run(args, rig, state_dir)[0] == 0


def saved_sessions(state_dir, match="m1"):
    return json.loads((state_dir / f"{match}.json").read_text())["sessions"]


def write_state(state_dir, sessions, match="m1"):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{match}.json").write_text(json.dumps({"match_id": match, "sessions": sessions}))


def legacy_session(**extra):
    return {
        "runtime": "harness",
        "name": "harness-1",
        "team": "red",
        "session_id": "ctf-m1-harness-1-" + "a" * 32,
        "runtime_arn": ARN.format("harness"),
        "ack": "started",
        **extra,
    }


class TestSessionLiveness:
    """An ended session must never be invoked: InvokeAgentRuntime boots a fresh, empty microVM."""

    def test_start_records_started_at_last_seen_and_last_status_in_utc(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args)
        (session,) = saved_sessions(state_dir)
        assert session["last_status"] == "started"
        assert session["started_at"] == session["last_seen"] == rig.clock().isoformat()
        assert session["started_at"].endswith("+00:00")

    def test_a_successful_status_refreshes_last_status_and_last_seen(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args)
        rig.clock.advance(60)
        rig.invoke("harness", reply={"status": "running", "ticks": 3})
        assert run(["status", "--match", "m1"], rig, state_dir)[0] == 0
        (session,) = saved_sessions(state_dir)
        assert session["last_status"] == "running"
        assert session["last_seen"] == rig.clock().isoformat()
        assert session["started_at"] != session["last_seen"]

    def test_a_failed_status_call_leaves_last_status_alone(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args)
        rig.invoke("harness", error="ThrottlingException")
        assert run(["status", "--match", "m1"], rig, state_dir)[0] == 1
        assert saved_sessions(state_dir)[0]["last_status"] == "started"

    @pytest.mark.parametrize("terminal", ["finished", "failed", "stopped"])
    def test_status_does_not_invoke_a_session_whose_last_status_is_terminal(
        self, rig, state_dir, arn_args, capsys, terminal
    ):
        start_one(rig, state_dir, arn_args)
        rig.invoke("harness", reply={"status": terminal})
        run(["status", "--match", "m1"], rig, state_dir)  # learns the terminal status
        capsys.readouterr()
        before = rig.invoked
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == before
        assert "ended (not queried)" in out.out and terminal in out.out
        assert out.err == ""

    def test_status_does_not_invoke_stopped_failed_or_aged_out_sessions(self, rig, state_dir, capsys):
        write_state(
            state_dir,
            [
                legacy_session(name="a", stop="requested", last_status="stopping"),
                legacy_session(name="b", ack="failed", error="ThrottlingException"),
                legacy_session(
                    name="c",
                    started_at=rig.clock().isoformat(),
                    last_seen=rig.clock().isoformat(),
                    last_status="running",
                ),
            ],
        )
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS + 1)
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == 0
        assert out.out.count("ended (not queried)") == 3
        assert "stopping" in out.out and "running" in out.out

    def test_exactly_at_the_max_lifetime_is_still_alive_one_second_later_is_not(
        self, rig, state_dir, arn_args, capsys
    ):
        start_one(rig, state_dir, arn_args)
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS)
        rig.invoke("harness", reply={"status": "running"})
        assert run(["status", "--match", "m1"], rig, state_dir)[0] == 0  # queried, last_seen is now
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS + 1)
        before = rig.invoked
        _, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert rig.invoked == before and "ended (not queried)" in out.out

    def test_old_state_files_without_timestamps_are_queried_not_guessed(self, rig, state_dir, capsys):
        write_state(state_dir, [legacy_session()])
        rig.invoke("harness", reply={"status": "running"})
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == 1 and "running" in out.out

    def test_last_seen_alone_ages_a_session_out_when_started_at_is_missing(self, rig, state_dir, capsys):
        write_state(state_dir, [legacy_session(last_seen=rig.clock().isoformat(), last_status="running")])
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS + 1)
        code, out = run(["status", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == 0 and "ended (not queried)" in out.out

    def test_wake_forces_a_query_of_an_ended_session(self, rig, state_dir, arn_args, capsys):
        start_one(rig, state_dir, arn_args)
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS + 1)
        before = rig.invoked
        rig.invoke("harness", reply={"status": "idle"})
        code, out = run(["status", "--match", "m1", "--wake"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == before + 1
        assert "ended (not queried)" not in out.out and "idle" in out.out

    def test_cooperative_stop_is_sent_only_to_sessions_that_may_be_alive(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args)
        rig.invoke("harness", reply={"status": "stopping"})
        before = rig.invoked
        assert run(["stop", "--match", "m1", "--yes"], rig, state_dir)[0] == 0
        assert rig.invoked == before + 1
        (session,) = saved_sessions(state_dir)
        assert session["stop"] == "requested" and session["last_status"] == "stopping"

    @pytest.mark.parametrize("reason", ["aged_out", "terminal", "stopped", "failed_start"])
    def test_ended_sessions_are_stopped_with_stop_runtime_session_never_invoked(self, rig, state_dir, reason):
        extra = {
            "aged_out": {"started_at": rig.clock().isoformat(), "last_status": "running"},
            "terminal": {"last_status": "finished"},
            "stopped": {"stop": "requested", "last_status": "stopping"},
            "failed_start": {"ack": "failed"},
        }[reason]
        write_state(state_dir, [legacy_session(**extra)])
        rig.clock.advance(fleet_config.MAX_LIFETIME_SECONDS + 1)
        rig.stop_session("harness")
        assert run(["stop", "--match", "m1", "--yes"], rig, state_dir)[0] == 0
        assert rig.invoked == 0
        assert saved_sessions(state_dir)[0]["stop"] == "hard"

    def test_resource_not_found_on_stop_runtime_session_is_success(self, rig, state_dir, capsys):
        write_state(state_dir, [legacy_session(last_status="finished")])
        rig.stop_session_error("harness", "ResourceNotFoundException")
        code, out = run(["stop", "--match", "m1", "--yes"], rig, state_dir, capsys)
        assert code == 0 and out.err == "" and rig.invoked == 0
        assert saved_sessions(state_dir)[0]["stop"] == "hard"

    def test_hard_stop_always_uses_stop_runtime_session_and_tolerates_not_found(
        self, rig, state_dir, arn_args
    ):
        start_one(rig, state_dir, arn_args)  # alive, yet --hard never invokes
        rig.stop_session_error("harness", "ResourceNotFoundException")
        before = rig.invoked
        assert run(["stop", "--match", "m1", "--hard", "--yes"], rig, state_dir)[0] == 0
        assert rig.invoked == before

    def test_other_stop_runtime_session_errors_are_still_failures(self, rig, state_dir, capsys):
        write_state(state_dir, [legacy_session(last_status="finished")])
        rig.stop_session_error("harness", "AccessDeniedException")
        code, out = run(["stop", "--match", "m1", "--yes"], rig, state_dir, capsys)
        assert code == 1 and "AccessDeniedException" in out.err
        assert "stop" not in saved_sessions(state_dir)[0]

    def test_stop_dry_run_makes_no_call_at_all(self, rig, state_dir, capsys):
        write_state(state_dir, [legacy_session(last_status="finished")])
        code, out = run(["stop", "--match", "m1"], rig, state_dir, capsys)
        assert code == 0 and rig.invoked == 0 and "dry run" in out.out.lower()


class TestStateSafety:
    def test_stopped_sessions_do_not_count_toward_the_per_type_cap(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args, count=3)
        for _ in range(3):
            rig.stop_session("harness")
        assert run(["stop", "--match", "m1", "--hard", "--yes"], rig, state_dir)[0] == 0
        start_one(rig, state_dir, arn_args, count=3)
        assert [s["name"] for s in saved_sessions(state_dir)] == [f"harness-{i}" for i in range(1, 7)]

    def test_cap_still_counts_sessions_that_were_not_stopped(self, rig, state_dir, arn_args, capsys):
        start_one(rig, state_dir, arn_args, count=3)
        sessions = saved_sessions(state_dir)
        sessions[0]["stop"] = "hard"  # as left by an earlier stop
        write_state(state_dir, sessions)
        start_one(rig, state_dir, arn_args, count=1)  # 2 active + 1 = 3: allowed
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir, capsys
        )
        assert code == 2 and "at most 3" in out.err

    @pytest.mark.parametrize(
        "argv",
        [
            ["status", "--match", "../evil"],
            ["stop", "--match", "../evil", "--yes"],
            ["stop", "--match", "a/b", "--hard"],
            ["start", "--match", "../evil", "--agent", "harness=1", "--yes"],
            ["status", "--match", ""],
        ],
    )
    def test_match_ids_are_validated_in_every_subcommand(self, rig, state_dir, tmp_path, capsys, argv):
        code, out = run(argv, rig, state_dir, capsys)
        assert code == 2 and "match id" in out.err
        assert list(tmp_path.rglob("evil*")) == []

    def test_a_state_path_can_never_leave_the_state_directory(self, state_dir):
        state = fleet.FleetState(state_dir)
        with pytest.raises(fleet.LaunchError, match="match id"):
            state.path("../escape")

    @pytest.mark.parametrize("content", ["{not json", "[]", '{"match_id": "m1"}', '{"sessions": {}}'])
    def test_corrupt_state_is_a_clear_error_not_a_traceback(self, rig, state_dir, capsys, content):
        state_dir.mkdir()
        (state_dir / "m1.json").write_text(content)
        for argv in (["status", "--match", "m1"], ["status"], ["stop", "--all"], ["stop", "--match", "m1"]):
            code, out = run(argv, rig, state_dir, capsys)
            assert code == 2, argv
            assert "m1.json" in out.err and "Traceback" not in out.err, argv

    def test_start_refuses_to_overwrite_a_corrupt_state_file(self, rig, state_dir, arn_args, capsys):
        state_dir.mkdir()
        (state_dir / "m1.json").write_text("{not json")
        code, out = run(
            ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"], rig, state_dir, capsys
        )
        assert code == 2 and "m1.json" in out.err
        assert (state_dir / "m1.json").read_text() == "{not json"
        assert rig.invoked == 0

    def test_a_lock_file_guards_each_match(self, rig, state_dir, arn_args):
        start_one(rig, state_dir, arn_args)
        assert (state_dir / "m1.lock").is_file()
        assert [p.name for p in state_dir.glob("*.json")] == ["m1.json"]  # lock files are not state

    def test_the_lock_is_exclusive_between_threads(self, state_dir):
        state = fleet.FleetState(state_dir)
        entered = threading.Event()

        def contender():
            with state.locked("m1"):
                entered.set()

        with state.locked("m1"):
            thread = threading.Thread(target=contender)
            thread.start()
            assert not entered.wait(0.3)
        assert entered.wait(5)
        thread.join()

    def test_start_waits_for_the_match_lock_before_it_loads_state(self, rig, state_dir, arn_args):
        state = fleet.FleetState(state_dir)
        rig.invoke("harness")
        result = []
        args = ["start", "--match", "m1", "--agent", "harness=1", *arn_args, "--yes"]
        with state.locked("m1"):
            thread = threading.Thread(target=lambda: result.append(run(args, rig, state_dir)[0]))
            thread.start()
            time.sleep(0.3)
            assert rig.invoked == 0
        thread.join(5)
        assert result == [0] and rig.invoked == 1
