"""Task 0.4 / S3, step 2: mint ONE real M2M token through AgentCore Identity.

MUTATING and AWS-side: creates a workload identity and (optionally) an OAuth2
credential provider in the account. Run it only on a laptop with the deployer's
credentials. It never prints the secret or the token; it prints decoded claims only.

Prerequisites (read-only facts verified in PR0 from the CLI 0.31.1 source):
  * `agentcore deploy` creates the OAuth2 credential provider named exactly like the
    credential in agentcore.json, reading AGENTCORE_CREDENTIAL_<NAME>_CLIENT_ID /
    _CLIENT_SECRET from agentcore/.env.local (NAME upper-cased, '-' -> '_', no project prefix).
  * So the simplest end-to-end check is: add ONE credential (ctf_harness) to agentcore.json,
    hydrate .env.local for it, run `agentcore deploy` once, then run this script with
    PROVIDER=ctf_harness. See spikes/README.md section "S3".

Usage:
  PROVIDER=ctf_harness AWS_REGION=us-east-2 \
  uv run --python 3.13 --with 'bedrock-agentcore>=1.24.1' python spikes/s3_identity_token.py
Pass criteria (record in blog notes): token minted, scope contains agentic-ctf/mcp, token_use == access,
and the `sub` differs per provider when run with a second PROVIDER.
"""
import base64
import json
import os
import sys

from bedrock_agentcore.services.identity import IdentityClient


def _claims(jwt: str) -> dict:
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> int:
    provider = os.environ.get("PROVIDER")
    if not provider:
        print("error: set PROVIDER to the credential provider name (e.g. PROVIDER=ctf_harness)", file=sys.stderr)
        return 2
    region = os.environ.get("AWS_REGION", "us-east-2")
    scopes = os.environ.get("SCOPES", "agentic-ctf/mcp").split()
    workload = os.environ.get("WORKLOAD_NAME", f"spike-{provider}")

    client = IdentityClient(region)
    try:
        client.create_workload_identity(name=workload)          # mutating, idempotency not guaranteed
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        if code == "ConflictException" or "already exists" in str(exc).lower():
            print("create_workload_identity: already exists, continuing")
        else:
            print("create_workload_identity failed:", code, file=sys.stderr)
            raise
    workload_token = client.get_workload_access_token(workload_name=workload)["workloadAccessToken"]

    import asyncio

    token = asyncio.run(
        client.get_token(
            provider_name=provider,
            agent_identity_token=workload_token,
            scopes=scopes,
            auth_flow="M2M",
        )
    )
    claims = _claims(token)
    print(json.dumps({k: claims.get(k) for k in ("sub", "client_id", "scope", "token_use", "iss", "exp", "iat")}, indent=2))
    ok = "agentic-ctf/mcp" in str(claims.get("scope", "")) and claims.get("token_use") == "access"
    print("S3 TOKEN CHECK:", "PASS" if ok else "FAIL")
    if "exp" in claims and "iat" in claims:
        print("token lifetime seconds:", claims["exp"] - claims["iat"], "(expected 14400 once D6 is deployed; 3600 means D6 not applied yet)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
