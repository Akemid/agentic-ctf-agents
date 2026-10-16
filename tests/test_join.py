"""The join signal (first outcome wins) and the pure parsing of a `join_match` tool result."""

import json
import threading

import pytest

from ctf_common.join import (
    JoinSignal,
    join_reason,
    report_join_result,
    seat_in_code,
)
from ctf_common.mcp import open_mcp_client
from ctf_common.payload import Action, MatchRequest
from fake_mcp import FakeGame, in_memory_transport

TIMEOUT = 5


def result(payload, *, error=False):
    """The shape strands / MCPClient return for a tool call."""
    return {
        "status": "error" if error else "success",
        "toolUseId": "t1",
        "content": [{"text": payload if isinstance(payload, str) else json.dumps(payload)}],
    }


class TestJoinSignal:
    def test_nothing_is_decided_until_someone_reports(self):
        signal = JoinSignal()
        assert signal.outcome is None and signal.wait(0) is None

    def test_success_carries_the_assigned_team_and_slot(self):
        signal = JoinSignal()
        assert signal.succeed("blue", 2) is True
        outcome = signal.wait(0)
        assert (outcome.kind, outcome.team, outcome.agent_index) == ("joined", "blue", 2)

    def test_the_first_outcome_wins_and_later_ones_are_ignored(self):
        signal = JoinSignal()
        assert signal.refuse("slot_taken") is True
        assert signal.succeed("red", 0) is False
        assert signal.error("boom") is False
        assert signal.cancel() is False
        outcome = signal.outcome
        assert (outcome.kind, outcome.reason) == ("refused", "slot_taken")

    @pytest.mark.parametrize("report", ["refuse", "error", "cancel"])
    def test_every_outcome_wakes_a_waiter(self, report):
        signal = JoinSignal()
        woke = []
        thread = threading.Thread(target=lambda: woke.append(signal.wait(TIMEOUT)), daemon=True)
        thread.start()
        getattr(signal, report)(*(() if report == "cancel" else ("why",)))
        thread.join(TIMEOUT)
        assert woke and woke[0] is not None

    def test_expire_resolves_a_pending_join_as_an_error_and_returns_the_final_outcome(self):
        signal = JoinSignal()
        outcome = signal.expire("join_match timed out after 15s")
        assert (outcome.kind, outcome.reason) == ("error", "join_match timed out after 15s")

    def test_expire_keeps_a_join_that_won_the_race(self):
        signal = JoinSignal()
        signal.succeed("red", 1)
        assert signal.expire("late").kind == "joined"

    def test_wait_times_out_with_none(self):
        assert JoinSignal().wait(0.01) is None


class TestJoinReason:
    def test_a_tool_error_reason_is_taken_from_the_json_payload(self):
        assert join_reason(result({"reason": "slot_taken", "detail": "red-2 is taken"}, error=True)) == (
            "slot_taken"
        )

    def test_an_error_without_a_reason_is_summarised_not_dropped(self):
        assert join_reason(result("boom happened", error=True)) == "boom happened"

    def test_a_dead_connection_is_named(self):
        dead = {
            "status": "error",
            "content": [{"text": "Connection to the MCP server was closed"}],
        }
        assert join_reason(dead) == "connection_lost"

    def test_an_empty_error_is_unknown(self):
        assert join_reason({"status": "error", "content": []}) == "unknown"


class TestReportJoinResult:
    def test_a_successful_join_reports_the_servers_team_and_agent_index(self):
        signal = JoinSignal()
        report_join_result(signal, result({"agent_id": "blue-3", "team": "blue", "agent_index": 2}))
        outcome = signal.outcome
        assert (outcome.kind, outcome.team, outcome.agent_index) == ("joined", "blue", 2)

    @pytest.mark.parametrize(
        "reason",
        ["slot_taken", "match_full", "match_already_started", "match_finished", "unauthorized"],
    )
    def test_every_documented_refusal_becomes_a_refusal_with_its_reason(self, reason):
        signal = JoinSignal()
        report_join_result(signal, result({"reason": reason}, error=True))
        assert (signal.outcome.kind, signal.outcome.reason) == ("refused", reason)

    def test_an_is_error_flag_counts_even_without_the_error_status(self):
        signal = JoinSignal()
        report_join_result(signal, {**result({"reason": "slot_taken"}), "isError": True})
        assert signal.outcome.kind == "refused"

    @pytest.mark.parametrize(
        "body", ["not json", json.dumps({"team": "red"}), json.dumps({"agent_index": 0})]
    )
    def test_a_success_that_lacks_team_or_agent_index_is_an_error_not_a_guess(self, body):
        signal = JoinSignal()
        report_join_result(signal, result(body))
        assert signal.outcome.kind == "error"
        assert "join_match" in signal.outcome.reason


def request(team="red", agent_index=None):
    return MatchRequest(
        action=Action.START,
        match_id="m1",
        name="p1",
        team=team,
        model="haiku",
        options={},
        agent_index=agent_index,
    )


def seat(game, req):
    """seat_in_code against a real MCPClient over the in-memory fake server."""
    client = open_mcp_client(
        "https://x/mcp",
        type("T", (), {"get_token": lambda self: "t"})(),
        connect=lambda u, h: in_memory_transport(game.server),
    )
    signal = JoinSignal()
    with client:
        joined = seat_in_code(client, req, signal)
    return joined, signal


def join_calls(game):
    return [a for n, a in game.actions if n == "join_match"]


class TestSeatInCode:
    def test_the_join_is_made_with_the_requested_arguments_and_returns_the_join_result(self):
        game = FakeGame(turns=1)
        joined, signal = seat(game, request(team="blue", agent_index=2))
        assert join_calls(game) == [{"match_id": "m1", "name": "p1", "team": "blue", "agent_index": 2}]
        assert (signal.outcome.kind, signal.outcome.team, signal.outcome.agent_index) == ("joined", "blue", 2)
        assert joined["agent_index"] == 2 and "map" in joined

    def test_without_a_slot_no_agent_index_is_sent_and_the_servers_choice_is_reported(self):
        game = FakeGame(turns=1, taken_slots=(("red", 0),))
        _, signal = seat(game, request(team="red"))
        assert "agent_index" not in join_calls(game)[0]
        assert signal.outcome.agent_index == 1

    def test_a_taken_slot_is_refused_with_one_attempt_and_never_another_slot(self):
        game = FakeGame(turns=1, taken_slots=(("red", 1),))
        joined, signal = seat(game, request(team="red", agent_index=1))
        assert joined is None
        assert (signal.outcome.kind, signal.outcome.reason) == ("refused", "slot_taken")
        assert len(join_calls(game)) == 1

    def test_a_fixed_team_never_tries_the_other_team(self):
        game = FakeGame(turns=1, reject_teams=("blue",))
        _, signal = seat(game, request(team="blue"))
        assert signal.outcome.reason == "match_full" and [c["team"] for c in join_calls(game)] == ["blue"]

    def test_team_auto_tries_red_then_blue_after_a_full_team(self):
        game = FakeGame(turns=1, reject_teams=("red",))
        _, signal = seat(game, request(team="auto"))
        assert [c["team"] for c in join_calls(game)] == ["red", "blue"]
        assert (signal.outcome.team, signal.outcome.agent_index) == ("blue", 0)

    def test_team_auto_gives_up_when_both_teams_are_full(self):
        game = FakeGame(turns=1, full=True)
        _, signal = seat(game, request(team="auto"))
        assert [c["team"] for c in join_calls(game)] == ["red", "blue"]
        assert (signal.outcome.kind, signal.outcome.reason) == ("refused", "match_full")

    def test_team_auto_does_not_retry_after_another_reason(self):
        game = FakeGame(turns=1, started=True)
        _, signal = seat(game, request(team="auto"))
        assert len(join_calls(game)) == 1 and signal.outcome.reason == "match_already_started"

    def test_a_call_that_blows_up_is_an_error_not_a_crash(self):
        class Boom:
            def call_tool_sync(self, *a, **k):
                raise RuntimeError("socket closed")

        signal = JoinSignal()
        assert seat_in_code(Boom(), request(), signal) is None
        assert signal.outcome.kind == "error" and "socket closed" in signal.outcome.reason


class StopAfterCall:
    """Wraps a real client and sets `stop` once a join_match call went through."""

    def __init__(self, client, stop):
        self._client, self._stop = client, stop

    def call_tool_sync(self, *args, **kwargs):
        result = self._client.call_tool_sync(*args, **kwargs)
        self._stop.set()
        return result


class TestSeatHonoursStop:
    def open(self, game):
        return open_mcp_client(
            "https://x/mcp",
            type("T", (), {"get_token": lambda self: "t"})(),
            connect=lambda u, h: in_memory_transport(game.server),
        )

    def test_a_stop_set_before_the_first_call_takes_no_seat_and_cancels_the_join(self):
        game, signal, stop = FakeGame(turns=1), JoinSignal(), threading.Event()
        stop.set()
        with self.open(game) as client:
            assert seat_in_code(client, request(team="red"), signal, stop=stop) is None
        assert join_calls(game) == [] and signal.outcome.kind == "cancelled"

    def test_a_stop_between_the_auto_teams_never_tries_blue(self):
        game, signal, stop = FakeGame(turns=1, reject_teams=("red",)), JoinSignal(), threading.Event()
        with self.open(game) as client:
            joined = seat_in_code(StopAfterCall(client, stop), request(team="auto"), signal, stop=stop)
        assert joined is None
        assert [c["team"] for c in join_calls(game)] == ["red"]
        assert signal.outcome.kind == "cancelled"

    def test_without_a_stop_nothing_changes(self):
        game, signal = FakeGame(turns=1), JoinSignal()
        with self.open(game) as client:
            assert seat_in_code(client, request(team="red"), signal, stop=threading.Event()) is not None
