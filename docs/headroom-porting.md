# Porting patterns from `headroom`

We borrow a few operational patterns from **[headroom](https://github.com/chopratejas/headroom)**
(Apache-2.0) — a context-compression layer whose `wrap`/proxy machinery overlaps
with our CLI interception. headroom is large and fast-moving, and most of it
(compression, CCR, memory, `learn`) is irrelevant to us. We only track a narrow
slice: **CLI wrap, proxy lifecycle, and Copilot auth**.

This doc + `scripts/check_headroom_updates.py` let us notice when the specific
upstream code behind a borrowed (or planned) pattern changes, without re-reading
6k lines.

- **Upstream pin:** headroom **v0.29.0** (`headroom/_version.py`).
- **Snapshots:** `docs/headroom-porting/snapshots/` — verbatim source of each
  watched symbol at the pinned version (Apache-2.0, kept solely as a drift
  baseline; not our code, excluded from ruff/mypy).

## Checking for upstream updates

```bash
# 1. Get a fresh headroom (clone or release zip)
git clone --depth 1 https://github.com/chopratejas/headroom /tmp/headroom

# 2. Diff the watched symbols against our snapshots
python scripts/check_headroom_updates.py --headroom-dir /tmp/headroom

# 3. Re-review anything it flags. After porting (or deciding to skip), re-baseline:
python scripts/check_headroom_updates.py --headroom-dir /tmp/headroom --update
#    then bump HEADROOM_VERSION in the script and the pin above.
```

Exit code is non-zero on drift, so it can gate CI if we ever want that.

## Watch list — our component ↔ upstream symbol

Groups match the `group` tags in the script's `WATCHES`.

### A. Adopted (watch for improvements)

| Pattern | Our code | Upstream symbol | Notes |
|---|---|---|---|
| `ENABLE_TOOL_SEARCH` behind custom base URL | `proxy/cli_shims.py:_proxy_env_overrides` | `cli/wrap.py::_configure_tool_search_env`, `_normalize_tool_search_mode`, `providers/claude/runtime.py::TOOL_SEARCH_DEFAULT` | We only set the default; headroom also parses `auto:N`. If they change the accepted values, revisit. |
| Remote Control gate warning | `cli.py:_REMOTE_CONTROL_NOTE` | `providers/claude/runtime.py::remote_control_gate_message`, `REMOTE_CONTROL_DISABLED_MESSAGE` | Wording/behaviour of Claude's gate — mirror if upstream learns more. |

### B. Backlog — shared-proxy resilience on Windows

Only relevant if we move from "one in-process proxy per `vc`" to a **shared,
out-of-process proxy daemon**. See the backlog memory for the full rationale.

| Piece | Upstream symbol | Why |
|---|---|---|
| Detach flags (`CREATE_NO_WINDOW`/`NEW_PROCESS_GROUP`/`BREAKAWAY_FROM_JOB`) + fallback retry | `cli/wrap.py::_start_proxy` | Stop a terminal/Job close from tree-killing a shared proxy. |
| Marker-file reference counting | `cli/wrap.py::_make_cleanup`, `_live_proxy_clients`, `_register_proxy_client` | Kill the proxy only when the last client exits. |
| PID-reuse defense via process start-time | `cli/wrap.py::_marker_pid_reused`, `_proc_identity` | Don't trust a recycled PID's stale marker. |

### C. Backlog — Copilot CLI auth-reuse

Only needed if a base-URL redirect can't carry the CLI's own `Authorization`
(pending the Copilot spike). The portable, low-cost part is the **discovery map**
(`iter_oauth_token_candidates`); the token-exchange machinery is heavy and
Copilot-specific — keep as an optional extra, not core.

| Piece | Upstream symbol | Why |
|---|---|---|
| Where CLIs stash creds (env → Keychain → Cred Manager → `apps.json`/`hosts.json`) | `copilot_auth.py::iter_oauth_token_candidates` | Reusable "where's the auth" map. |
| OAuth → short-lived Copilot token exchange | `copilot_auth.py::_subscription_resolution_from_token_exchange`, `_copilot_token_exchange_headers`, `DEFAULT_TOKEN_EXCHANGE_URL` | Only if we must own upstream auth. |

## When you borrow something new

1. Add a `Watch(...)` row to `WATCHES` in `scripts/check_headroom_updates.py`.
2. Run `--update` to snapshot it.
3. Add a row to the table above.
