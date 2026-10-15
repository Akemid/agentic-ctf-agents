#!/usr/bin/env python3
"""Hydrate the AgentCore OAuth credentials before `agentcore deploy` (design A9, Path A).

The agentic-ctf stack publishes one Cognito client per agent: the client id in SSM and the
client secret in Secrets Manager. `agentcore deploy` reads them from the gitignored
`agentcore/.env.local` and creates one outbound OAuth credential provider per runtime.

    python scripts/hydrate_credentials.py                          # SSM + Secrets Manager -> .env.local
    python scripts/hydrate_credentials.py --update-discovery-url   # accept a changed Cognito pool
    python scripts/hydrate_credentials.py --check                  # offline pre-deploy check
    python scripts/hydrate_credentials.py --clean                  # wipe the secrets, restore the placeholder

Public-repo safety: secrets live only in `.env.local` (mode 0600, gitignored) and are never
printed. The committed `agentcore.json` carries a placeholder `discoveryUrl`; this script swaps
in the real Cognito URL for the deploy and `--clean` swaps the placeholder back, so the working
tree is clean again afterwards.

Read-only against AWS: SSM GetParameters and Secrets Manager GetSecretValue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fleet_config

DEFAULT_AGENTCORE_DIR = Path(__file__).resolve().parents[1] / "agentcore"
ENV_FILE = ".env.local"
SPEC_FILE = "agentcore.json"
SSM_BATCH = 10  # GetParameters limit
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_\-+/=.~]+$")  # safe in a dotenv line without quoting
DISCOVERY_SUFFIX = "/.well-known/openid-configuration"


class HydrateError(RuntimeError):
    """Hydration failed. The message names the agent and the parameter, never a secret value."""


@dataclass(frozen=True)
class HydrateReport:
    variables: int
    discovery: str  # "set" (placeholder replaced), "unchanged", or "updated" (drift accepted)
    env_path: Path


Clients = Callable[[str | None], tuple[Any, Any]]


def _our_variables() -> set[str]:
    return {name for item in fleet_config.FLEET for name in fleet_config.credential_env_vars(item)}


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", type(exc).__name__))
    return type(exc).__name__


def _fetch_parameters(ssm: Any, names: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for start in range(0, len(names), SSM_BATCH):
        chunk = names[start : start + SSM_BATCH]
        try:
            response = ssm.get_parameters(Names=chunk)
        except Exception as exc:  # noqa: BLE001
            raise HydrateError(f"cannot read SSM parameters: {_error_code(exc)}") from exc
        for parameter in response.get("Parameters", []):
            found[parameter["Name"]] = parameter["Value"]
    return found


def _read_secret(secretsmanager: Any, runtime: str, arn: str) -> dict[str, Any]:
    try:
        body = secretsmanager.get_secret_value(SecretId=arn)["SecretString"]
    except Exception as exc:  # noqa: BLE001
        raise HydrateError(f"{runtime}: cannot read its client secret ({_error_code(exc)})") from exc
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        raise HydrateError(f"{runtime}: the client secret is not a JSON object") from None
    if not isinstance(parsed, dict):
        raise HydrateError(f"{runtime}: the client secret is not a JSON object")
    return parsed


def _checked_value(runtime: str, what: str, value: Any) -> str:
    if not isinstance(value, str) or not SAFE_VALUE.fullmatch(value):
        raise HydrateError(f"{runtime}: {what} is empty or has characters that cannot go in a dotenv file")
    return value


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write via a sibling temp file so a failure never leaves a partial or world-readable file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_spec(agentcore_dir: Path) -> dict[str, Any]:
    return json.loads((agentcore_dir / SPEC_FILE).read_text())


def _dump_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def _credentials_by_name(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in spec.get("credentials", [])}


def _plan_discovery(spec: dict[str, Any], url: str, update: bool) -> str:
    credentials = _credentials_by_name(spec)
    missing = [i.credential for i in fleet_config.FLEET if i.credential not in credentials]
    if missing:
        raise HydrateError(f"agentcore.json has no credential named: {', '.join(missing)}")
    current = {credentials[i.credential].get("discoveryUrl", "") for i in fleet_config.FLEET}
    if current == {url}:
        return "unchanged"
    real = {c for c in current if not fleet_config.is_placeholder(c)}
    if real and not update:
        raise HydrateError(
            "discovery URL drift: agentcore.json differs from SSM "
            f"({fleet_config.DISCOVERY_URL_PARAM}). The Cognito pool may have been recreated. "
            "Re-run with --update-discovery-url to accept the SSM value."
        )
    return "updated" if real else "set"


def _set_discovery(agentcore_dir: Path, url: str, only_if: Callable[[str], bool] | None = None) -> None:
    spec = _load_spec(agentcore_dir)
    credentials = _credentials_by_name(spec)
    for item in fleet_config.FLEET:
        credential = credentials.get(item.credential)
        if credential is not None and (only_if is None or only_if(credential.get("discoveryUrl", ""))):
            credential["discoveryUrl"] = url
    path = agentcore_dir / SPEC_FILE
    _atomic_write(path, _dump_spec(spec), stat.S_IMODE(path.stat().st_mode))  # keep the file's own mode


def _read_env_lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _line_key(line: str) -> str:
    return line.partition("=")[0].strip()


def hydrate(
    ssm: Any,
    secretsmanager: Any,
    *,
    agentcore_dir: Path = DEFAULT_AGENTCORE_DIR,
    update_discovery_url: bool = False,
) -> HydrateReport:
    members = fleet_config.FLEET
    wanted = [fleet_config.DISCOVERY_URL_PARAM]
    for item in members:
        wanted.extend(fleet_config.agent_ssm_params(item))
    found = _fetch_parameters(ssm, wanted)

    problems: list[str] = []
    discovery = found.get(fleet_config.DISCOVERY_URL_PARAM, "")
    if not discovery:
        problems.append(f"SSM parameter {fleet_config.DISCOVERY_URL_PARAM} not found")
    elif not (discovery.startswith("https://") and discovery.endswith(DISCOVERY_SUFFIX)):
        problems.append(
            f"SSM parameter {fleet_config.DISCOVERY_URL_PARAM} is not an https discovery-url "
            f"ending in {DISCOVERY_SUFFIX}"
        )
    for item in members:
        problems.extend(
            f"{item.runtime}: SSM parameter {name} not found"
            for name in fleet_config.agent_ssm_params(item)
            if not found.get(name)
        )
    if problems:
        raise HydrateError("; ".join(problems))

    values: dict[str, str] = {}
    for item in members:
        id_param, arn_param = fleet_config.agent_ssm_params(item)
        id_var, secret_var = fleet_config.credential_env_vars(item)
        try:
            secret = _read_secret(secretsmanager, item.runtime, found[arn_param])
            client_id = _checked_value(item.runtime, "its client id", found[id_param])
            if "client_id" in secret and secret["client_id"] != found[id_param]:
                raise HydrateError(f"{item.runtime}: client_id in the secret differs from SSM {id_param}")
            if "client_secret" not in secret:
                raise HydrateError(f"{item.runtime}: the secret has no client_secret key")
            values[id_var] = client_id
            values[secret_var] = _checked_value(item.runtime, "its client secret", secret["client_secret"])
        except HydrateError as exc:
            problems.append(str(exc))
    if problems:
        raise HydrateError("; ".join(problems))

    spec = _load_spec(agentcore_dir)
    outcome = _plan_discovery(spec, discovery, update_discovery_url)  # raises on drift, before any write

    env_path = agentcore_dir / ENV_FILE
    kept = [line for line in _read_env_lines(env_path) if _line_key(line) not in _our_variables()]
    lines = kept + [
        f"{name}={values[name]}" for item in members for name in fleet_config.credential_env_vars(item)
    ]
    # The pair is written spec first, secrets second. If the second write fails, `--check` reports the
    # missing/empty .env.local and `--clean` restores the placeholder, so nothing half-done deploys.
    if outcome != "unchanged":
        _set_discovery(agentcore_dir, discovery)
    _atomic_write(env_path, "\n".join(lines) + "\n", 0o600)
    return HydrateReport(variables=len(values), discovery=outcome, env_path=env_path)


def clean(agentcore_dir: Path = DEFAULT_AGENTCORE_DIR) -> None:
    """Remove the hydrated variables (and the file if nothing else is left); restore the placeholder."""
    env_path = agentcore_dir / ENV_FILE
    if env_path.exists():
        kept = [line for line in _read_env_lines(env_path) if _line_key(line) not in _our_variables()]
        if any(line.strip() for line in kept):
            _atomic_write(env_path, "\n".join(kept) + "\n", 0o600)
        else:
            env_path.unlink()
    if (agentcore_dir / SPEC_FILE).exists():
        _set_discovery(
            agentcore_dir,
            fleet_config.DISCOVERY_PLACEHOLDER,
            only_if=lambda url: not fleet_config.is_placeholder(url),
        )


def check(agentcore_dir: Path = DEFAULT_AGENTCORE_DIR) -> list[str]:
    """Offline pre-deploy check: problems that would make `agentcore deploy` create broken providers."""
    problems: list[str] = []
    credentials = _credentials_by_name(_load_spec(agentcore_dir))
    for item in fleet_config.FLEET:
        url = credentials.get(item.credential, {}).get("discoveryUrl", "")
        if not url:
            problems.append(f"{item.credential}: no discoveryUrl in agentcore.json")
        elif fleet_config.is_placeholder(url):
            problems.append(
                f"{item.credential}: discoveryUrl is still the placeholder (run hydrate_credentials.py)"
            )
    env_path = agentcore_dir / ENV_FILE
    if not env_path.exists():
        problems.append(f"{ENV_FILE} does not exist (run hydrate_credentials.py)")
    present = {_line_key(line): line.partition("=")[2] for line in _read_env_lines(env_path)}
    for name in sorted(_our_variables()):
        if not present.get(name):
            problems.append(f"{name} is missing or empty in {ENV_FILE}")
    if env_path.exists() and stat.S_IMODE(env_path.stat().st_mode) != 0o600:
        problems.append(f"{ENV_FILE} must have mode 0600 (chmod 600)")
    return problems


def _boto_clients(region: str | None) -> tuple[Any, Any]:
    import boto3  # noqa: PLC0415

    return boto3.client("ssm", region_name=region), boto3.client("secretsmanager", region_name=region)


def main(argv: list[str] | None = None, *, clients: Clients | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="offline: verify .env.local and the discovery URL")
    mode.add_argument("--clean", action="store_true", help="remove the secrets and restore the placeholder")
    parser.add_argument(
        "--update-discovery-url", action="store_true", help="accept a changed Cognito discovery URL"
    )
    parser.add_argument("--agentcore-dir", type=Path, default=DEFAULT_AGENTCORE_DIR)
    parser.add_argument("--region", default=None, help="AWS region (default: the usual boto3 resolution)")
    args = parser.parse_args(argv)
    directory: Path = args.agentcore_dir

    try:
        if args.clean:
            clean(directory)
            print(f"removed hydrated credentials from {ENV_FILE}; discoveryUrl restored to the placeholder")
            return 0
        if args.check:
            problems = check(directory)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("credentials are ready for deploy" if not problems else "credentials are NOT ready")
            return 1 if problems else 0
        ssm, secretsmanager = (clients or _boto_clients)(args.region)
        report = hydrate(
            ssm, secretsmanager, agentcore_dir=directory, update_discovery_url=args.update_discovery_url
        )
    except HydrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (no credentials, no region, ...)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote {report.variables} variables to {ENV_FILE} (mode 0600); discoveryUrl: {report.discovery}. "
        "Deploy now, then run with --clean."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
