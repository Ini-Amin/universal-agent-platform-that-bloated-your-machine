# bugbounty-mcp — Tool Inventory & Integration Notes

Source: `/home/user/bugbounty-mcp` (Go, module `bugbounty-mcp`, go 1.27.1, mcp-go v1.1.1).
Status: verified by direct source reading (file paths + line refs below). Read-only inspection; no repo files modified.
Purpose of this note: input for UAP build Step 8 (MCP Integration, Master section 13) and Step 7 (Tool Registry, Master section 12).

## 1. Launch modes

| Mode | Command | Purpose |
|---|---|---|
| stdio MCP (default) | `./bbmcp` (no args, or any flags) | JSON-RPC 2.0 over stdin/stdout for Claude Desktop / Cursor / any MCP client |
| serve | `./bbmcp serve` | Web dashboard `http://127.0.0.1:8787/` + REST + SSE + MCP over HTTP |
| hook-guard | `./bbmcp hook-guard` | PreToolUse guardrail hook (used by the runner's claude subprocess) |

Evidence: `main.go` `firstCommand()` dispatch; stdio path in `runStdio()` (`main.go:52-185`).

Client connection (stdio) for UAP Tool Registry:
```json
{
  "mcpServers": {
    "bugbounty-mcp": {
      "type": "stdio",
      "command": "/home/user/bugbounty-mcp/bbmcp",
      "args": [],
      "env": { "CLAUDE_BIN": "claude" }
    }
  }
}
```
Notes: `CLAUDE_BIN` is optional (defaults to `claude`; only used by workflow tools). Server name reported in handshake: `bugbounty-mcp` v`1.0.0`, capabilities: tools.

## 2. Tool catalog (11 tools)

### Registered in stdio mode always (`main.go`)

| Tool | Inputs | Network? | Risk tier | What it does |
|---|---|---|---|---|
| `validate_scope` | `target` (str, req), `in_scope` (str[], req), `out_of_scope` (str[], opt) | No | 0 — pure local | Evaluates target against scope rules via `internal/scope.Engine.IsAllowed`. Returns `{target, allowed, reason}` as JSON text. |
| `lookup_dns` | `host` (str, req) | Yes (DNS 53) | 1 — passive | Resolves A/AAAA + CNAME via `recon.LookupDNS` (`internal/recon/dns.go`). Used for dangling-CNAME/takeover detection. |
| `find_subdomains_passive` | `domain` (str, req) | Yes (crt.sh only) | 1 — passive | Queries `https://crt.sh/?q=%25.<domain>&output=json` via `recon.FindSubdomains` (`internal/recon/crtsh.go`). Returns `{domain, count, subdomains[]}`. No packets to the target. |
| `probe_http` | `url` (str, req) | Yes (target!) | 2 — active | GET to target via `recon.ProbeHTTP` (`internal/recon/http.go`): 10s timeout, max 64KB body read, returns status code, HTML title, security headers. |

### Registered when `config.Discover()` succeeds (`internal/app/tools.go`, shared with serve mode)

| Tool | Inputs | Side effects | Risk tier | What it does |
|---|---|---|---|---|
| `list_agents` | — | Reads `~/.claude/agents` | 0 | Lists Claude Code agent definitions + sidecar metadata. |
| `list_skills` | — | Reads `~/.claude/skills` | 0 | Lists skills inventory. |
| `list_mcp_servers` | — | Reads MCP configs | 0 | Lists configured MCP servers. |
| `test_mcp_server` | `name` (str, req) | Spawns another MCP server process | 3 | Connects to the named MCP server over stdio and lists its tools (config verification). |
| `run_agent` | (see `internal/app/tools.go:145`) | Spawns `claude` CLI subprocess | 3 | Runs a Claude Code agent as a subprocess; returns a run id monitorable via SSE. |
| `get_run` | run id | None (read state) | 0 | Fetches run status/timeline. |
| `stop_run` | run id | Kills a subprocess | 3 | Stops a running agent run. |

## 3. Scope-guard behavior (`internal/scope/scope.go`)

- `Engine.IsAllowed(target)`: normalizes the target (strips scheme/path/port to hostname), then:
  1. **Out-of-scope has strict precedence** — any match → `blocked: matched out-of-scope rule <rule>`.
  2. **Empty in-scope list blocks everything** — `blocked: no in-scope rules defined` (fail-closed).
  3. Wildcard matching: `*.domain.com` matches root and any subdomain; otherwise exact host match (case-insensitive).
- Blocking is reported as a normal tool result with `{"allowed": false, "reason": "blocked: ..."}` — not as a JSON-RPC error. UAP's policy guard must treat `allowed=false` as a hard stop (Master section 9: policy enforced outside LLM reasoning).
- Scope rules are passed **per call** to `validate_scope` (in_scope/out_of_scope arrays), not read from a global file in this code path. The dashboard's per-agent guardrail (in/out-of-scope, deniedTools, requireApproval, maxBudget, rate limit) lives in `internal/guardrail` (`policy.go`, `hook.go`) and applies to the claude subprocess runner, not to MCP tool calls.

## 4. Risk tiers for UAP Tool Registry

| Tier | Meaning | Tools | UAP handling |
|---|---|---|---|
| 0 | Pure local, deterministic | `validate_scope`, `list_agents`, `list_skills`, `list_mcp_servers`, `get_run` | Auto-allow; no approval. |
| 1 | Passive external (third-party services, no target traffic) | `lookup_dns`, `find_subdomains_passive` | Allow; log. Requires in-scope domain for the program context. |
| 2 | Active against target | `probe_http` | Allow only after `validate_scope` allows; log + rate-limit. |
| 3 | Subprocess / side-effectful | `run_agent`, `stop_run`, `test_mcp_server` | Requires explicit approval state (Master section 20) + budget/rate policy. |

## 5. Uncertainties / follow-ups

- `run_agent` input schema not fully read (params beyond name); confirm at Step 8 wiring time from `internal/app/tools.go:145-195`.
- The dashboard guardrail policy file location/format was not read (`internal/guardrail/policy.go`); confirm if UAP wants to reuse it.
- No multi-source passive recon (single crt.sh endpoint — flagged by the repo's own `ponytail` comment).
