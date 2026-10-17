# Delta Spec: mcp-m2m-auth

## ADDED Requirements

### Requirement: Token requests equal MCP connections
An OAuth token MUST be requested only by the function that opens the MCP connection (decorated with `@requires_access_token(auth_flow="M2M")`), never by individual tool calls, because the decorator has no client-side cache (D6). The `Authorization` header MUST be reused for every request within that connection.

#### Scenario: Many tool calls, one token
- GIVEN one MCP connection and 200 tool calls
- WHEN `GetResourceOauth2Token` invocations are counted
- THEN exactly 1 was made

#### Scenario: Two connections
- GIVEN an agent that opens 2 MCP connections
- WHEN tokens are counted
- THEN exactly 2 were requested

#### Scenario: No decorator on tools
- GIVEN the agent source
- WHEN scanned
- THEN `requires_access_token` appears only on connection-opening functions

### Requirement: Per-runtime credential provider
Each runtime MUST use its own OAuth credential provider (Cognito discovery URL, its own client id, scope `agentic-ctf/mcp`), so each has a distinct client and `sub`.

#### Scenario: Provider mapping
- GIVEN four runtimes
- WHEN their provider names are read
- THEN four distinct providers exist, each bound to one runtime

#### Scenario: Scope
- GIVEN a minted token
- WHEN decoded
- THEN scope contains `agentic-ctf/mcp` and `token_use` is `access`

### Requirement: No mid-match reconnect logic
The design relies on the access-token validity of the agent M2M Cognito client (4 h, set by agentic-ctf in `cognito-auth.ts`; PR0 found 1 h on branch `feat/m2m-agent-clients`, change to 4 h requested 2026-10-14 per decision D6) outliving a match; every MCP request re-verifies the bearer token, so a match MUST finish within 4 h of its token being fetched. The agent MUST NOT implement refresh or reconnect-on-401 loops. A 401 MUST fail the match with a logged auth error.

#### Scenario: Token outlived
- GIVEN a match still running 4 h after its token was fetched
- WHEN the next MCP request returns 401
- THEN the match fails with an auth reason (no refresh is attempted)

#### Scenario: 401 surfaces
- GIVEN the MCP returns 401
- WHEN the agent sees it
- THEN the match status becomes `failed` with an auth reason, and no new token is requested

#### Scenario: Mid-match 401
- GIVEN the MCP handshake and `tools/list` succeeded and the match is running
- WHEN a later `tools/call` returns HTTP 401 (Strands reports it to the model only as a closed-connection tool error)
- THEN the agent loop ends after that tool batch (the model is not left retrying), the match status becomes `failed` with an auth reason, and no new token is requested

### Requirement: Secrets never logged or committed
Client secrets MUST NOT appear in logs, CloudWatch, payloads, or tracked files; `agentcore/.env.local` MUST be gitignored.

#### Scenario: Gitignore
- GIVEN the repository
- WHEN `.env.local` is checked
- THEN it is ignored

#### Scenario: Log redaction
- GIVEN a match with a token
- WHEN logs are scanned
- THEN neither the token nor secret appears
