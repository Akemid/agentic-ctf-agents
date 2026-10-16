#!/usr/bin/env python3
"""Fleet launcher: start, watch and stop CTF characters across the AgentCore runtimes.

    fleet.py start  --match M --agent decider_loop=2 --agent harness_sdk=1 [--teams 2] [--model haiku] [--yes]
    fleet.py start  --match M --agent decider_loop=1 --team red --agent-index 1 [--yes]  # an exact seat
    fleet.py status [--match M] [--wake]
    fleet.py stop   (--match M | --all) [--hard] [--yes]

One invocation plays one character. Every character gets its own session id (>= 33 chars) and
its own `InvokeAgentRuntime` call carrying `runtimeUserId` (the SDK needs it to mint the
workload token). Sessions are recorded in `.fleet/<match>.json` (gitignored) so `status` and
`stop` address exactly the sessions this launcher started. Every session also keeps its last
known status and the time it was last seen.

Ended sessions are never invoked: `InvokeAgentRuntime` on a session that is gone boots a fresh,
empty microVM (a cost, and a misleading `idle`). A session counts as ended when it was stopped,
its start failed, its last status is terminal (finished, failed, stopped) or it is older than the
runtime max lifetime. `status` prints `ended (not queried)` for those (`--wake` queries anyway)
and `stop` ends them with `StopRuntimeSession` only.

Safety: `start` and `stop` print a plan and change nothing unless `--yes` is given. Limits
(3 characters per agent type, 2 teams) are checked before the first invocation. Runtime ARNs
come from `--runtime-arn RUNTIME=ARN`, else from `agentcore/.cli/deployed-state.json` (local,
gitignored: it holds account ARNs), else from the SSM parameters `/ctfagents/runtimes/<runtime>/arn`
that the AgentCore stack publishes (needs `ssm:GetParameters` on them).

`start` is synchronous up to the seat: the runtime answers only after `join_match`, so a started
character prints the team and agent_index the server really gave it, and a refused seat
(`slot_taken`, `match_full`, ...) is a failed start with the reason. `--agent-index N` (0..2, the name and
range of the server's `join_match`; `--slot` is an equivalent option, the web lobby's key) takes exactly one
seat and needs `--team red|blue`; a requested seat is never traded for another one.

Exit codes: 0 ok, 1 at least one invocation failed, 2 the request was rejected (nothing invoked).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fleet_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / ".fleet"
DEFAULT_DEPLOYED_STATE = ROOT / "agentcore" / ".cli" / "deployed-state.json"
DEFAULT_USER_ID = "fleet-launcher"
SSM_ARN_PREFIX = "/ctfagents/runtimes"
AGENT_INDEX_MAX = 2
EXPLICIT_TEAMS = ("red", "blue")
MODELS = ("haiku", "opus", "nova-lite")  # mirrors ctf_common.payload.MODELS (contract-tested)
MATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
START_ACKS = frozenset({"started", "already_running"})
TERMINAL_STATUSES = frozenset({"finished", "failed", "stopped"})
Clock = Callable[[], datetime]
ERROR_PREVIEW = 200


class LaunchError(ValueError):
    """The request is invalid. Raised before anything is invoked."""


@dataclass(frozen=True)
class Character:
    runtime: str
    name: str
    team: str
    session_id: str
    match_id: str
    agent_index: int | None = None


# --- planning (pure) ---------------------------------------------------------------------------


def _is_active(session: Mapping[str, Any]) -> bool:
    """Counts toward the per-type cap: started (or not yet failed) and not stopped."""
    return session.get("ack") != "failed" and not session.get("stop")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _aged_out(session: Mapping[str, Any], now: datetime) -> bool:
    """Older than the runtime max lifetime. Sessions with no timestamps (old state files) are not guessed."""
    reference = _parse_time(session.get("started_at")) or _parse_time(session.get("last_seen"))
    return reference is not None and (now - reference).total_seconds() > fleet_config.MAX_LIFETIME_SECONDS


def may_be_alive(session: Mapping[str, Any], now: datetime) -> bool:
    return not (
        session.get("stop")
        or session.get("ack") == "failed"
        or session.get("last_status") in TERMINAL_STATUSES
        or _aged_out(session, now)
    )


def plan_launch(
    match_id: str,
    counts: Mapping[str, int],
    teams: int,
    *,
    existing: Sequence[Mapping[str, Any]] = (),
    token: Callable[[], str] = lambda: uuid.uuid4().hex,
    team: str | None = None,
    agent_index: int | None = None,
) -> list[Character]:
    """Validate a launch request and expand it into characters. Raises LaunchError, never invokes.

    `team` (red|blue) pins every character to that team instead of alternating. `agent_index` (0..2, the
    server's own name and range) also needs an explicit team and exactly one character: it names the lobby
    seat, and the character is named after it (`<runtime>-<team>-<agent_index+1>`, the server's scheme).
    """
    if not MATCH_ID_RE.fullmatch(match_id):
        raise LaunchError("match id must match ^[A-Za-z0-9_-]{1,64}$")
    if teams < 1:
        raise LaunchError("teams must be at least 1")
    if teams > fleet_config.MAX_TEAMS:
        raise LaunchError(f"at most {fleet_config.MAX_TEAMS} teams are supported")
    for runtime, count in counts.items():
        try:
            fleet_config.member(runtime)
        except KeyError:
            raise LaunchError(f"unknown runtime: {runtime}") from None
        if count < 0:
            raise LaunchError(f"{runtime}: negative count")
    if sum(counts.values()) == 0:
        raise LaunchError("no characters requested")
    if team not in (None, "auto", *EXPLICIT_TEAMS):
        raise LaunchError("team must be red, blue or auto")
    if agent_index is not None:
        if team not in EXPLICIT_TEAMS:
            raise LaunchError("--agent-index needs --team red or --team blue (a seat belongs to one team)")
        if not 0 <= agent_index <= AGENT_INDEX_MAX:
            raise LaunchError(f"agent_index must be between 0 and {AGENT_INDEX_MAX}")
        if sum(counts.values()) != 1:
            raise LaunchError("--agent-index targets exactly one character")
    pinned_team = team if team in EXPLICIT_TEAMS else None

    active_total = sum(1 for s in existing if _is_active(s))
    characters: list[Character] = []
    for item in fleet_config.FLEET:  # fleet order keeps the plan and the state file deterministic
        count = counts.get(item.runtime, 0)
        if count == 0:
            continue
        recorded = [s for s in existing if s["runtime"] == item.runtime]
        active = sum(1 for s in recorded if _is_active(s))
        if active + count > fleet_config.MAX_CHARACTERS_PER_TYPE:
            raise LaunchError(
                f"{item.runtime}: at most {fleet_config.MAX_CHARACTERS_PER_TYPE} characters per agent type "
                f"({active} already running in this match, {count} requested)"
            )
        base = item.runtime.replace("_", "-")
        for offset in range(count):
            if agent_index is not None:
                name = f"{base}-{pinned_team}-{agent_index + 1}"
            else:
                name = f"{base}-{len(recorded) + offset + 1}"
            side = pinned_team or ("red" if (active_total + len(characters)) % teams == 0 else "blue")
            session_id = f"ctf-{match_id}-{name}-{token()}"
            characters.append(Character(item.runtime, name, side, session_id, match_id, agent_index))
    return characters


def build_payload(character: Character, *, model: str = "haiku") -> dict[str, Any]:
    """The `start` payload (contract: ctf_common.payload.parse_payload)."""
    payload: dict[str, Any] = {
        "action": "start",
        "match_id": character.match_id,
        "name": character.name,
        "team": character.team,
        "model": model,
    }
    if character.agent_index is not None:
        payload["agent_index"] = character.agent_index
    return payload


# --- runtime ARNs ------------------------------------------------------------------------------


SsmFactory = Callable[[str | None], Any]


def resolve_runtime_arns(
    needed: Sequence[str],
    overrides: Mapping[str, str],
    deployed_state: Path,
    target: str | None,
    *,
    ssm_factory: SsmFactory | None = None,
    ssm_region: str | None = None,
) -> dict[str, str]:
    """`--runtime-arn` first, then deployed-state, then SSM for whatever is still missing."""
    arns = {rt: overrides[rt] for rt in needed if rt in overrides}
    missing = [rt for rt in needed if rt not in arns]
    if not missing:
        return arns
    problem = ""
    if not deployed_state.is_file():
        problem = f"{deployed_state.name} not found"
    else:
        try:
            targets = json.loads(deployed_state.read_text()).get("targets", {})
        except ValueError:
            raise LaunchError(f"{deployed_state.name} is not valid JSON") from None
        if target is None:
            if len(targets) != 1:
                raise LaunchError(
                    f"{deployed_state.name} has {len(targets)} targets: choose one with --target"
                )
            target = next(iter(targets))
        runtimes = targets.get(target, {}).get("resources", {}).get("runtimes", {})
        arns.update(
            {rt: runtimes[rt]["runtimeArn"] for rt in missing if "runtimeArn" in runtimes.get(rt, {})}
        )
        missing = [rt for rt in missing if rt not in arns]
        if not missing:
            return arns
        problem = f"runtime(s) not in deployed-state target '{target}': {', '.join(missing)}"
    arns.update(_arns_from_ssm(missing, problem, ssm_factory, ssm_region))
    return arns


def _arns_from_ssm(
    missing: Sequence[str], problem: str, ssm_factory: SsmFactory | None, region: str | None
) -> dict[str, str]:
    """Read `/ctfagents/runtimes/<runtime>/arn`. Any failure explains every way out, never a traceback."""
    names = {f"{SSM_ARN_PREFIX}/{rt}/arn": rt for rt in missing}
    hint = (
        f"no runtime ARN for {', '.join(missing)} ({problem}). Run `agentcore deploy` first, "
        "pass --runtime-arn RUNTIME=ARN"
    )
    try:
        client = (ssm_factory or _default_ssm_factory)(region)
        reply = client.get_parameters(Names=list(names))
    except Exception as exc:  # noqa: BLE001 - credentials, permissions, throttling: all end in a clear rejection
        raise LaunchError(
            f"{hint}, or allow ssm:GetParameters on {SSM_ARN_PREFIX}/* (SSM: {_error_code(exc)})"
        ) from None
    found = {names[p["Name"]]: p["Value"] for p in reply.get("Parameters", []) if p.get("Name") in names}
    absent = [rt for rt in missing if rt not in found]
    if absent:
        wanted = ", ".join(f"{SSM_ARN_PREFIX}/{rt}/arn" for rt in absent)
        raise LaunchError(f"{hint}, or deploy the stack that publishes {wanted}")
    return found


# --- state -------------------------------------------------------------------------------------


class FleetState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, match_id: str) -> Path:
        if not MATCH_ID_RE.fullmatch(match_id):
            raise LaunchError("match id must match ^[A-Za-z0-9_-]{1,64}$")
        return self.directory / f"{match_id}.json"

    def exists(self, match_id: str) -> bool:
        return self.path(match_id).is_file()

    @contextlib.contextmanager
    def locked(self, match_id: str) -> Iterator[None]:
        """Exclusive advisory lock around one match's load-modify-save (`.fleet/<match>.lock`)."""
        self.path(match_id)  # validates the id before it names a file
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / f"{match_id}.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            record = json.loads(path.read_text())
        except ValueError as exc:
            raise LaunchError(
                f"state file {path.name} is not valid JSON ({exc}); repair or delete it"
            ) from None
        if not isinstance(record, dict) or not isinstance(record.get("sessions"), list):
            raise LaunchError(f"state file {path.name} is malformed: expected an object with a sessions list")
        if not all(isinstance(session, dict) for session in record["sessions"]):
            raise LaunchError(f"state file {path.name} is malformed: sessions must be objects")
        record.setdefault("match_id", path.stem)
        return record

    def load(self, match_id: str) -> dict[str, Any]:
        path = self.path(match_id)
        if not path.is_file():
            raise LaunchError(f"no recorded sessions for match '{match_id}' in {self.directory.name}/")
        return self._read(path)

    def match_ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return [p.stem for p in sorted(self.directory.glob("*.json")) if MATCH_ID_RE.fullmatch(p.stem)]

    def save(self, record: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(record["match_id"])
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# --- invocation --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    ok: bool
    status: str
    ticks: Any = None
    error: str = ""
    answered: bool = False  # the runtime replied (even with a bad status); False for transport errors
    team: str | None = None  # the seat the runtime reported on `started` (what the server really assigned)
    agent_index: int | None = None


def _region_of(arn: str) -> str:
    return arn.split(":")[3]


def _invoke(
    client: Any, arn: str, session_id: str, body: Mapping[str, Any], user_id: str
) -> Mapping[str, Any]:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps(body).encode(),
        runtimeUserId=user_id,
        contentType="application/json",
        accept="application/json",
    )
    raw = response["response"].read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise RuntimeError("reply is not JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("reply is not a JSON object")
    return parsed


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", type(exc).__name__))
    return str(exc)[:ERROR_PREVIEW] or type(exc).__name__


def _call(
    clients: ClientPool,
    arn: str,
    session_id: str,
    body: Mapping[str, Any],
    user_id: str,
    accepted: frozenset[str] | None,
) -> Reply:
    try:
        reply = _invoke(clients.get(arn), arn, session_id, body, user_id)
    except Exception as exc:  # noqa: BLE001
        return Reply(False, "failed", error=_error_code(exc))
    status = str(reply.get("status", "unknown"))
    ok = status not in {"rejected", "failed", "unknown"} if accepted is None else status in accepted
    return Reply(
        ok,
        status,
        ticks=reply.get("ticks"),
        error=str(reply.get("error", ""))[:ERROR_PREVIEW],
        answered=True,
        team=reply.get("team") if isinstance(reply.get("team"), str) else None,
        agent_index=_reply_agent_index(reply),
    )


def _reply_agent_index(reply: Mapping[str, Any]) -> int | None:
    """`agent_index` is canonical; `slot` is the equivalent key the runtime also replies with."""
    for key in ("agent_index", "slot"):
        value = reply.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


class ClientPool:
    def __init__(self, factory: Callable[[str], Any]) -> None:
        self._factory = factory
        self._clients: dict[str, Any] = {}

    def get(self, arn: str) -> Any:
        region = _region_of(arn)
        if region not in self._clients:
            self._clients[region] = self._factory(region)
        return self._clients[region]


def _default_factory(region: str) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.client("bedrock-agentcore", region_name=region)


def _default_ssm_factory(region: str | None) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.client("ssm", region_name=region)


# --- commands ----------------------------------------------------------------------------------


def _parse_agents(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        runtime, sep, number = value.partition("=")
        if not sep or not number.lstrip("-").isdigit():
            raise LaunchError(f"--agent expects RUNTIME=COUNT, got '{value}'")
        counts[runtime] = counts.get(runtime, 0) + int(number)
    return counts


def _parse_arns(values: Sequence[str]) -> dict[str, str]:
    arns: dict[str, str] = {}
    for value in values:
        runtime, sep, arn = value.partition("=")
        if not sep or not arn.startswith("arn:") or arn.count(":") < 5:
            raise LaunchError(f"--runtime-arn expects RUNTIME=ARN, got '{runtime or value}'")
        try:
            fleet_config.member(runtime)
        except KeyError:
            raise LaunchError(f"unknown runtime: {runtime}") from None
        arns[runtime] = arn
    return arns


def _failure_line(session: Mapping[str, Any], what: str, reply: Reply) -> str:
    detail = reply.error or reply.status
    return f"  {session['runtime']}/{session['name']}: {what} failed: {detail}"


def _stamp(session: dict[str, Any], reply: Reply, now: datetime) -> None:
    """Remember the last status a session answered with. Only a reply from the runtime proves it was alive."""
    if reply.answered:
        session["last_status"] = reply.status
        session["last_seen"] = now.isoformat()


def _load_or_new(state: FleetState, match_id: str) -> dict[str, Any]:
    return state.load(match_id) if state.exists(match_id) else {"match_id": match_id, "sessions": []}


def cmd_start(
    args: argparse.Namespace,
    state: FleetState,
    pool: ClientPool,
    clock: Clock,
    ssm_factory: SsmFactory | None = None,
) -> int:
    counts = _parse_agents(args.agent)
    overrides = _parse_arns(args.runtime_arn)
    # Validate and show the plan without taking the lock or creating anything (the dry run).
    existing = _load_or_new(state, args.match)["sessions"]
    plan = plan_launch(
        args.match, counts, args.teams, existing=existing, team=args.team, agent_index=args.agent_index
    )
    resolved = resolve_runtime_arns(
        sorted({c.runtime for c in plan}),
        overrides,
        args.deployed_state,
        args.target,
        ssm_factory=ssm_factory,
        ssm_region=args.region,
    )
    _print_plan(plan, args)
    if not args.yes:
        print("Dry run: nothing was invoked. Add --yes to start these characters.")
        return 0

    with state.locked(args.match):
        record = _load_or_new(state, args.match)  # authoritative: re-read and re-plan under the lock
        plan = plan_launch(
            args.match,
            counts,
            args.teams,
            existing=record["sessions"],
            team=args.team,
            agent_index=args.agent_index,
        )
        needed = sorted({c.runtime for c in plan})
        # The re-plan under the lock rarely changes the runtimes: do not ask SSM a second time for them.
        arns = {rt: resolved[rt] for rt in needed if rt in resolved}
        if len(arns) < len(needed):
            arns = resolve_runtime_arns(
                needed,
                overrides,
                args.deployed_state,
                args.target,
                ssm_factory=ssm_factory,
                ssm_region=args.region,
            )
        failures: list[str] = []
        for character in plan:
            arn = arns[character.runtime]
            body = build_payload(character, model=args.model)
            reply = _call(pool, arn, character.session_id, body, args.user_id, START_ACKS)
            session: dict[str, Any] = {
                "runtime": character.runtime,
                "name": character.name,
                # `started` carries the seat the server really assigned: that wins over the plan.
                "team": (reply.team if reply.ok and reply.team else character.team),
                "session_id": character.session_id,
                "runtime_arn": arn,
                "ack": reply.status if reply.ok else "failed",
            }
            if reply.ok and reply.agent_index is not None:
                session["agent_index"] = reply.agent_index
            if reply.ok:
                session["started_at"] = clock().isoformat()
            _stamp(session, reply, clock())
            if not reply.ok:
                session["error"] = reply.error or reply.status
                failures.append(_failure_line(session, "start", reply))
            record["sessions"].append(session)
            state.save(record)  # after every character: an interrupted launch is still stoppable
            print(f"  {character.name}: {_start_outcome(reply)}")
    if failures:
        print(f"{len(failures)} of {len(plan)} start(s) failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Started {len(plan)} character(s). Check them with: fleet.py status --match {args.match}")
    return 0


def _start_outcome(reply: Reply) -> str:
    """One line per character: `ok (started) team red agent_index 1`, or the reason the runtime refused."""
    if not reply.ok:
        return f"FAILED: {reply.error or reply.status}"
    seat = ""
    if reply.team is not None and reply.agent_index is not None:
        seat = f" team {reply.team} agent_index {reply.agent_index}"
    return f"ok ({reply.status}){seat}"


def _print_plan(plan: Sequence[Character], args: argparse.Namespace) -> None:
    print(f"Plan: {len(plan)} character(s) for match '{args.match}', model {args.model}")
    for character in plan:
        seat = "" if character.agent_index is None else f" agent_index {character.agent_index}"
        print(f"  {character.runtime:<15} {character.name:<20} team {character.team}{seat}")


def _selected_matches(args: argparse.Namespace, state: FleetState) -> list[str]:
    if not getattr(args, "all", False) and args.match:
        state.load(args.match)  # fails early and clearly for an unknown or corrupt match
        return [args.match]
    ids = state.match_ids()
    if not ids:
        raise LaunchError(f"no recorded matches in {state.directory.name}/")
    for match_id in ids:
        state.load(match_id)  # a corrupt file is reported before anything is sent
    return ids


def cmd_status(args: argparse.Namespace, state: FleetState, pool: ClientPool, clock: Clock) -> int:
    failures: list[str] = []
    for match_id in _selected_matches(args, state):
        with state.locked(match_id):
            record = state.load(match_id)
            print(f"Match {record['match_id']}:")
            for session in record["sessions"]:
                who = f"{session['runtime']:<15} {session['name']:<20} {session['team']:<5}"
                if not args.wake and not may_be_alive(session, clock()):
                    last = session.get("last_status") or "-"
                    print(f"  {who} ended (not queried), last status: {last}")
                    continue
                arn, sid = session["runtime_arn"], session["session_id"]
                reply = _call(pool, arn, sid, {"action": "status"}, args.user_id, None)
                ticks = "-" if reply.ticks is None else reply.ticks
                print(f"  {who} {reply.status:<14} ticks {ticks}")
                _stamp(session, reply, clock())
                if not reply.ok:
                    failures.append(_failure_line(session, "status", reply))
            state.save(record)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


def _hard_stop(pool: ClientPool, session: Mapping[str, Any]) -> Reply:
    """StopRuntimeSession. A session that no longer exists is already stopped: ResourceNotFound is success."""
    try:
        pool.get(session["runtime_arn"]).stop_runtime_session(
            agentRuntimeArn=session["runtime_arn"], runtimeSessionId=session["session_id"]
        )
    except Exception as exc:  # noqa: BLE001
        code = _error_code(exc)
        if code.startswith("ResourceNotFound"):
            return Reply(True, "stopped")
        return Reply(False, "failed", error=code)
    return Reply(True, "stopped")


def cmd_stop(args: argparse.Namespace, state: FleetState, pool: ClientPool, clock: Clock) -> int:
    if not args.all and not args.match:
        raise LaunchError("stop needs --match MATCH_ID or --all")
    match_ids = _selected_matches(args, state)
    records = [state.load(m) for m in match_ids]
    total = sum(len(r["sessions"]) for r in records)
    way = "hard stop (StopRuntimeSession)" if args.hard else "stop request"
    print(f"Plan: {way} for {total} session(s) in {len(records)} match(es)")
    if not args.yes:
        for record in records:
            for session in record["sessions"]:
                ended = not args.hard and not may_be_alive(session, clock())
                note = " (ended: StopRuntimeSession only)" if ended else ""
                print(f"  {record['match_id']}: {session['runtime']}/{session['name']}{note}")
        print("Dry run: nothing was sent. Add --yes to stop them.")
        return 0

    failures: list[str] = []
    for match_id in match_ids:
        with state.locked(match_id):
            record = state.load(match_id)
            for session in record["sessions"]:
                hard = args.hard or not may_be_alive(session, clock())
                reply = (
                    _hard_stop(pool, session)
                    if hard
                    else _call(
                        pool,
                        session["runtime_arn"],
                        session["session_id"],
                        {"action": "stop"},
                        args.user_id,
                        None,
                    )
                )
                if reply.ok:
                    session["stop"] = "hard" if hard else "requested"
                    session["last_status"] = reply.status
                    session["last_seen"] = clock().isoformat()
                else:
                    failures.append(_failure_line(session, "stop", reply))
            state.save(record)
    if failures:
        print(f"{len(failures)} of {total} stop(s) failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Stopped {total} session(s).")
    return 0


# --- CLI ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="runtimeUserId sent on every invocation")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start characters for a match")
    start.add_argument("--match", required=True)
    start.add_argument("--agent", action="append", default=[], metavar="RUNTIME=COUNT")
    start.add_argument(
        "--teams", type=int, default=2, help="1 (all red) or 2 (alternate red/blue); default 2"
    )
    start.add_argument(
        "--team",
        choices=("red", "blue", "auto"),
        default=None,
        help="pin every character to a team (default: alternate by --teams)",
    )
    start.add_argument(
        "--agent-index",
        "--slot",  # equivalent alias (the web lobby's key)
        dest="agent_index",
        type=int,
        default=None,
        help="take this lobby seat (0..2, the server's agent_index) of --team; exactly one character, "
        "never another seat (--slot is an equivalent option)",
    )
    start.add_argument(
        "--region", default=None, help="region of the SSM parameters (default: the AWS default)"
    )
    start.add_argument("--model", choices=MODELS, default="haiku")
    start.add_argument("--runtime-arn", action="append", default=[], metavar="RUNTIME=ARN")
    start.add_argument("--deployed-state", type=Path, default=DEFAULT_DEPLOYED_STATE)
    start.add_argument("--target", default=None)
    start.add_argument("--yes", action="store_true", help="really invoke (default is a dry run)")

    status = sub.add_parser("status", help="ask every recorded session for its status")
    status.add_argument("--match", default=None)
    status.add_argument(
        "--wake", action="store_true", help="query ended sessions too (this can boot a fresh empty microVM)"
    )

    stop = sub.add_parser("stop", help="stop recorded sessions")
    stop.add_argument("--match", default=None)
    stop.add_argument("--all", action="store_true")
    stop.add_argument("--hard", action="store_true", help="StopRuntimeSession instead of a cooperative stop")
    stop.add_argument("--yes", action="store_true", help="really stop (default is a dry run)")
    return parser


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], Any] | None = None,
    clock: Clock = _utc_now,
    ssm_factory: SsmFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    state = FleetState(args.state_dir)
    pool = ClientPool(client_factory or _default_factory)
    try:
        if args.match is not None:
            state.path(args.match)  # every subcommand rejects a bad match id before touching anything
        if args.command == "start":
            return cmd_start(args, state, pool, clock, ssm_factory)
        commands = {"status": cmd_status, "stop": cmd_stop}
        return commands[args.command](args, state, pool, clock)
    except LaunchError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
