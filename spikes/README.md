# Spikes (PR0)

Investigative scripts for `agentcore-ctf-fleet`. Results and design decisions: `FINDINGS.md`.
Nothing here is imported by runtime code. Tests: `uv run --with pytest pytest spikes/tests`.

## Already executed (no action needed)
- S1 `s1_harness_headers.py`: `uv run --python 3.13 --with strands-harness --with mcp python spikes/s1_harness_headers.py` -> PASS.
- S2 / S5 / S3 criteria a-d / open questions: source and resolver evidence in `FINDINGS.md`.

## You must run (they mutate AWS or need a GPU)

### 0.1 SageMaker quota (DONE: request approved 2026-10-14, value 1.0)
```bash
bash spikes/s0_request_quota.sh status     # applied value must be >= 1
```

### 0.4 S3 real token (after AgenticCtfStack is deployed with agent clients; creates AWS resources)
Prerequisite: `/agentic-ctf/mcp-url` and `/agentic-ctf/agents/<name>/{client-id,secret-arn}` exist in SSM.
1. In `agentcore/agentcore.json` add ONE credential (the real file edit lands in PR4; for the spike do it locally):
   `{"authorizerType":"OAuthCredentialProvider","name":"ctf_harness","vendor":"CustomOauth2","usage":"outbound","scopes":["agentic-ctf/mcp"],"discoveryUrl":"<value of /agentic-ctf/cognito/discovery-url>"}`
2. Write `agentcore/.env.local` (gitignored, mode 600) WITHOUT echoing secrets:
   ```bash
   cd agentcore && umask 077
   CID=$(aws ssm get-parameter --name /agentic-ctf/agents/<name>/client-id --query Parameter.Value --output text)
   SARN=$(aws ssm get-parameter --name /agentic-ctf/agents/<name>/secret-arn --query Parameter.Value --output text)
   CSEC=$(aws secretsmanager get-secret-value --secret-id "$SARN" --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["client_secret"])')
   printf 'AGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_ID=%s\nAGENTCORE_CREDENTIAL_CTF_HARNESS_CLIENT_SECRET=%s\n' "$CID" "$CSEC" > .env.local
   ```
3. `agentcore deploy` (creates the provider), then `cat agentcore/.cli/deployed-state.json | grep -c credentialProviderArn` (criterion b).
4. `PROVIDER=ctf_harness AWS_REGION=us-east-2 uv run --python 3.13 --with 'bedrock-agentcore>=1.24.1' python spikes/s3_identity_token.py`
5. Cleanup: `rm agentcore/.env.local`; revert the local agentcore.json edit (or `agentcore remove credential`).
Pass: `S3 TOKEN CHECK: PASS`; lifetime 14400 once D6 (4 h) is deployed (found 1 h / 3600 in PR0; 4 h requested (D6); re-verify before E2E (task 4.5)). Any failure of a-d means Path B (see design A9).

### 0.7 S4 decider on a T4 (billable if on EC2)
On any NVIDIA T4 host (EC2 `g4dn.xlarge` Deep Learning AMI, or a Colab T4 runtime), copy `spikes/` and one decider log
(for example `amazingplayer-python/logs/decider-194227.log`), then:
```bash
bash spikes/s4_t4_host.sh ./decider-194227.log
```
Return `s4-work/bench-*.txt`, `compare.txt`, `vram-*.txt`, `gpu.txt`. Gates: p95 <= 1000 ms, fp16 vs fp32 delta <= 0.05.
InService time (<= 20 min) is measured on the first PR3 endpoint deploy.
