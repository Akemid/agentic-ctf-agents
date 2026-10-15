"""Payload validation (spec: match-session-lifecycle, payload validation, one invocation = one character)."""

import json
from dataclasses import FrozenInstanceError

import pytest

from ctf_common.payload import Action, MatchRequest, PayloadError, parse_payload

SESSION = "sess-0123456789-abcdef-0123456789-xyz123"  # last 6 chars: xyz123


def parse(raw, runtime="harness_sdk", session=SESSION, **kw):
    return parse_payload(raw, runtime=runtime, session_id=session, **kw)


class TestDefaults:
    def test_start_with_only_match_id_gets_defaults(self):
        req = parse({"match_id": "m1"})
        assert req == MatchRequest(
            action=Action.START,
            match_id="m1",
            name="harness_sdk-xyz123",
            team="auto",
            model="haiku",
            options={},
        )

    def test_explicit_values_are_kept(self):
        req = parse({"action": "start", "match_id": "m-2_X", "name": "p1", "team": "blue", "model": "opus"})
        assert (req.match_id, req.name, req.team, req.model) == ("m-2_X", "p1", "blue", "opus")

    def test_action_defaults_to_start_and_other_actions_need_no_match_id(self):
        assert parse({"match_id": "m1"}).action is Action.START
        assert parse({"action": "status"}).action is Action.STATUS
        assert parse({"action": "stop"}).action is Action.STOP

    def test_default_name_uses_last_six_chars_of_session_and_runtime(self):
        assert parse({"match_id": "m1"}, runtime="decider_loop", session="a" * 40 + "ABC-12").name == (
            "decider_loop-abc-12"
        )

    def test_default_name_is_capped_at_32_chars(self):
        name = parse({"match_id": "m1"}, runtime="r" * 40).name
        assert len(name) == 32 and name == name.lower()


class TestPromptUnwrap:
    def test_prompt_wrapped_json_is_unwrapped(self):
        req = parse({"prompt": json.dumps({"action": "status"})})
        assert req.action is Action.STATUS

    def test_prompt_wrapped_start(self):
        req = parse({"prompt": json.dumps({"match_id": "m9", "name": "p9"})})
        assert (req.action, req.match_id, req.name) == (Action.START, "m9", "p9")

    @pytest.mark.parametrize("prompt", [123, None, ["x"], {"action": "status"}])
    def test_non_string_prompt_is_rejected(self, prompt):
        with pytest.raises(PayloadError, match="prompt"):
            parse({"prompt": prompt})

    def test_prompt_that_is_not_json_is_rejected(self):
        with pytest.raises(PayloadError, match="prompt"):
            parse({"prompt": "play the game"})

    def test_prompt_json_that_is_not_an_object_is_rejected(self):
        with pytest.raises(PayloadError, match="object"):
            parse({"prompt": "[1, 2]"})

    def test_prompt_mixed_with_other_keys_is_rejected(self):
        with pytest.raises(PayloadError, match="action"):
            parse({"prompt": "{}", "action": "status"})

    def test_wrapped_payload_is_validated_like_a_plain_one(self):
        with pytest.raises(PayloadError, match="messages"):
            parse({"prompt": json.dumps({"match_id": "m1", "messages": []})})

    def test_prompt_cannot_be_nested_twice(self):
        inner = json.dumps({"prompt": json.dumps({"action": "status"})})
        with pytest.raises(PayloadError, match="prompt"):
            parse({"prompt": inner})


class TestRejections:
    @pytest.mark.parametrize("key", ["messages", "tool_results", "history"])
    def test_caller_supplied_history_is_rejected(self, key):
        with pytest.raises(PayloadError, match=key):
            parse({"match_id": "m1", key: [{"role": "user", "content": [{"text": "hi"}]}]})

    def test_unknown_key_is_rejected_and_named(self):
        with pytest.raises(PayloadError, match="character"):
            parse({"match_id": "m1", "character": "alice"})

    def test_payload_must_be_an_object(self):
        for raw in ("text", 1, None, [{"action": "start"}]):
            with pytest.raises(PayloadError, match="object"):
                parse(raw)

    def test_unknown_action(self):
        with pytest.raises(PayloadError, match="action"):
            parse({"action": "restart", "match_id": "m1"})

    def test_action_must_be_a_string(self):
        with pytest.raises(PayloadError, match="action"):
            parse({"action": 1, "match_id": "m1"})

    def test_start_requires_match_id_and_names_the_field(self):
        with pytest.raises(PayloadError, match="match_id"):
            parse({"action": "start"})
        with pytest.raises(PayloadError, match="match_id"):
            parse({"name": "p1"})

    @pytest.mark.parametrize("match_id", ["", "a b", "x" * 65, "ok/../no", "café", 7, ["m1"]])
    def test_bad_match_id(self, match_id):
        with pytest.raises(PayloadError, match="match_id"):
            parse({"match_id": match_id})

    def test_match_id_boundaries_are_accepted(self):
        assert parse({"match_id": "a"}).match_id == "a"
        assert parse({"match_id": "A" * 64}).match_id == "A" * 64

    def test_match_id_is_validated_on_status_when_present(self):
        with pytest.raises(PayloadError, match="match_id"):
            parse({"action": "status", "match_id": "bad id"})

    @pytest.mark.parametrize("team", ["green", "RED", 1])
    def test_bad_team(self, team):
        with pytest.raises(PayloadError, match="team"):
            parse({"match_id": "m1", "team": team})

    @pytest.mark.parametrize("model", ["gpt", "Haiku", None])
    def test_bad_model(self, model):
        with pytest.raises(PayloadError, match="model"):
            parse({"match_id": "m1", "model": model})


class TestSingleCharacter:
    def test_name_list_is_rejected(self):
        with pytest.raises(PayloadError, match="name"):
            parse({"match_id": "m1", "name": ["a", "b"]})

    def test_names_alias_keys_are_unknown(self):
        with pytest.raises(PayloadError, match="names"):
            parse({"match_id": "m1", "names": ["a", "b"]})

    def test_name_is_sanitized(self):
        assert parse({"match_id": "m1", "name": "Alice The Great!"}).name == "alice-the-great-"

    def test_name_is_truncated_to_32(self):
        assert parse({"match_id": "m1", "name": "n" * 50}).name == "n" * 32

    def test_name_that_sanitizes_to_nothing_is_rejected(self):
        with pytest.raises(PayloadError, match="name"):
            parse({"match_id": "m1", "name": ""})


class TestOptions:
    def test_valid_options_for_decider_loop(self):
        req = parse(
            {
                "match_id": "m1",
                "options": {
                    "threshold": 0.7,
                    "threat_threshold": 0.4,
                    "llm_budget_ms": 8000,
                    "safety_margin_ms": 500,
                },
            },
            runtime="decider_loop",
        )
        assert req.options == {
            "threshold": 0.7,
            "threat_threshold": 0.4,
            "llm_budget_ms": 8000,
            "safety_margin_ms": 500,
        }

    def test_window_for_harness_sdk(self):
        assert parse({"match_id": "m1", "options": {"window": 8}}).options == {"window": 8}

    def test_option_not_supported_by_runtime_is_rejected(self):
        with pytest.raises(PayloadError, match="threshold"):
            parse({"match_id": "m1", "options": {"threshold": 0.5}}, runtime="harness_sdk")
        with pytest.raises(PayloadError, match="window"):
            parse({"match_id": "m1", "options": {"window": 8}}, runtime="decider_loop")

    def test_unknown_option_is_rejected(self):
        with pytest.raises(PayloadError, match="speed"):
            parse({"match_id": "m1", "options": {"speed": 1}})

    @pytest.mark.parametrize(
        "options",
        [
            {"window": 0},
            {"window": 1001},
            {"window": "8"},
            {"window": 8.5},
            {"window": True},
        ],
    )
    def test_window_limits_and_types(self, options):
        with pytest.raises(PayloadError, match="window"):
            parse({"match_id": "m1", "options": options})

    @pytest.mark.parametrize(
        "options",
        [{"threshold": -0.1}, {"threshold": 1.1}, {"threshold": "0.5"}, {"threat_threshold": 2}],
    )
    def test_threshold_limits(self, options):
        with pytest.raises(PayloadError, match="threshold"):
            parse({"match_id": "m1", "options": options}, runtime="decider_loop")

    def test_integer_threshold_in_range_is_accepted_as_float(self):
        req = parse({"match_id": "m1", "options": {"threshold": 1}}, runtime="decider_loop")
        assert req.options == {"threshold": 1.0} and isinstance(req.options["threshold"], float)

    @pytest.mark.parametrize(
        "options", [{"llm_budget_ms": 0}, {"llm_budget_ms": 10**9}, {"safety_margin_ms": -1}]
    )
    def test_budget_limits(self, options):
        with pytest.raises(PayloadError, match="_ms"):
            parse({"match_id": "m1", "options": options}, runtime="decider_loop")

    def test_options_must_be_an_object(self):
        with pytest.raises(PayloadError, match="options"):
            parse({"match_id": "m1", "options": [1]})


def test_match_request_is_immutable():
    req = parse({"match_id": "m1"})
    with pytest.raises(FrozenInstanceError):
        req.match_id = "other"  # type: ignore[misc]
