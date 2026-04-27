# RFC 0001: CLI Companion Interface v0.1

Status: proposed

## Abstract

This RFC defines a small local integration contract between a coding CLI and a
companion service such as Voice Copilot.

The interface allows a CLI to:

- enable or disable companion integration at runtime
- emit only the event categories the companion has subscribed to
- expose one or more live sessions with stable identifiers
- accept commands such as interrupt, pause, resume, and follow-up input

The interface is explicitly designed for companion products that perform
parallel analysis and summarization with a separate lightweight LLM. It is not
designed for verbatim playback of hidden chain-of-thought or full raw answers.

The normative JSON Schema for this RFC lives in
[schemas/cli-companion-interface.schema.json](schemas/cli-companion-interface.schema.json).

## Status Of This Memo

This RFC is intended as a practical integration target for CLI authors and
companion-tool authors. It is strict enough to implement against, but still
early enough to refine together with maintainers of real coding CLIs.

## Normative Language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY in this document are
to be interpreted as described in RFC 2119.

## Goals

This interface MUST make it possible for a coding CLI to:

- opt into companion support without embedding a full voice UI
- expose only the events a companion actually needs
- let the companion send a minimal set of control commands
- support multiple host UIs through one local companion runtime

This interface SHOULD be useful for:

- narration and audio observability
- readable live trace views
- accessibility tooling
- VS Code and browser-based hosts
- future ecosystem tools beyond Voice Copilot

## Non-Goals

This RFC does not require:

- any specific transport implementation
- exposure of hidden model reasoning
- any specific voice, STT, or TTS provider
- a specific UI shell
- a specific coding model vendor

## Roles

- CLI: the coding agent process or terminal application
- Companion service: a local runtime such as Voice Copilot that consumes CLI
  events, runs summarization, manages TTS/STT, and routes commands
- Host UI: any client of the companion service, such as a browser popup, a
  VS Code extension, or another interface

The CLI communicates only with the companion service. Host UIs communicate only
with the companion service.

## Transport And Framing

This RFC is transport-agnostic.

Recommended transports for v0.1:

- local WebSocket
- stdio JSON lines
- named pipe or domain socket

Each protocol message MUST be a single JSON object. Multiple messages MUST NOT
be batched into one envelope.

## Versioning

- `protocol_version` is REQUIRED in `hello` and `hello_ack`
- v0.1 implementations MUST send `"0.1"`
- minor backwards-compatible extensions MAY add optional fields
- breaking changes MUST use a new protocol version

## Session Model

Each live dialog MUST have a stable `session_id`.

Recommended session properties:

- `session_id`: stable identifier for one live conversation/session
- `title`: optional human-readable label
- `cwd`: optional working directory
- `active`: whether the session is currently selected inside the CLI

The companion MUST treat `session_id` as the routing key for narration,
commands, pause/resume, and UI selection.

## Capability Model

The CLI MUST declare capabilities during handshake.

Defined capability fields in v0.1:

- `subscriptions`: CLI supports runtime subscription changes
- `interrupt`: CLI supports interrupting current work
- `pause`: CLI supports pausing execution
- `resume`: CLI supports resuming execution
- `send_user_message`: CLI supports receiving follow-up user messages
- `select_session`: CLI supports selecting the active session
- `session_selection_events`: CLI emits active-session changes
- `tool_events`: CLI emits tool lifecycle events
- `file_events`: CLI emits file edit events
- `thinking_events`: CLI emits optional thinking summaries or thinking-like
  progress updates
- `message_delivery`: one of `native`, `queue`, or `manual`

`message_delivery` has the following semantics:

- `native`: the CLI can inject a user message immediately mid-turn
- `queue`: the CLI can queue a message for the next turn boundary
- `manual`: the CLI cannot inject automatically; the companion must fall back
  to manual delivery

## Handshake

### CLI -> Companion

```json
{
  "type": "hello",
  "protocol_version": "0.1",
  "cli_name": "opencode",
  "cli_version": "1.2.3",
  "instance_id": "opencode-8f6f",
  "capabilities": {
    "subscriptions": true,
    "interrupt": true,
    "pause": true,
    "resume": true,
    "send_user_message": true,
    "select_session": true,
    "session_selection_events": true,
    "tool_events": true,
    "file_events": true,
    "thinking_events": false,
    "message_delivery": "native"
  }
}
```

### Companion -> CLI

```json
{
  "type": "hello_ack",
  "protocol_version": "0.1",
  "enabled": true,
  "subscriptions": [
    "session.*",
    "turn.*",
    "user.message",
    "agent.output",
    "tool.*",
    "file.edited",
    "error"
  ]
}
```

If `enabled` is `false`, the CLI MUST keep the control channel alive and MUST
stop sending optional high-volume events until re-enabled.

## Lifecycle Control

The companion MUST be able to toggle integration without forcing the CLI to
restart.

### Enable Or Disable Companion Support

```json
{
  "type": "control.set_enabled",
  "enabled": false
}
```

When disabled, the CLI SHOULD:

- stop sending optional stream events
- keep the control channel alive
- accept a later `enabled: true`

### Update Event Subscriptions

```json
{
  "type": "control.set_subscriptions",
  "subscriptions": [
    "session.*",
    "turn.*",
    "agent.output",
    "tool.*",
    "file.edited"
  ]
}
```

The CLI SHOULD emit only the subscribed event categories when subscriptions are
supported.

## Message Fields

### Common event fields

All event messages MUST include:

- `type: "event"`
- `kind`
- `session_id`
- `payload`

Event messages MAY include:

- `event_id`
- `ts` in RFC 3339 / ISO 8601 format

### Common command fields

All command messages MUST include:

- `type: "command"`
- `name`
- `session_id`

Command messages MAY include:

- `request_id` for correlation
- `payload`

### Command results

Command result messages MUST include:

- `type: "command_result"`
- `name`
- `session_id`
- `ok`

Command result messages SHOULD include `request_id` if the original command had
one.

## Event Kinds

The following event kinds are defined in v0.1.

### Session lifecycle

- `session.started`
- `session.ended`
- `session.selected`

Examples:

```json
{ "type": "event", "kind": "session.started", "session_id": "abc123", "payload": { "title": "Fix auth bug", "cwd": "F:/repo" } }
{ "type": "event", "kind": "session.ended", "session_id": "abc123", "payload": {} }
{ "type": "event", "kind": "session.selected", "session_id": "abc123", "payload": { "active": true } }
```

### Turn lifecycle

- `turn.started`
- `turn.ended`

Examples:

```json
{ "type": "event", "kind": "turn.started", "session_id": "abc123", "payload": {} }
{ "type": "event", "kind": "turn.ended", "session_id": "abc123", "payload": { "status": "ok" } }
```

### User and agent activity

- `user.message`
- `agent.output`
- `agent.awaiting_input`
- optional `agent.thinking`

Examples:

```json
{ "type": "event", "kind": "user.message", "session_id": "abc123", "payload": { "text": "Add tests for login" } }
{ "type": "event", "kind": "agent.output", "session_id": "abc123", "payload": { "text": "I found the failing test and I am updating the fixture." } }
{ "type": "event", "kind": "agent.awaiting_input", "session_id": "abc123", "payload": {} }
{ "type": "event", "kind": "agent.thinking", "session_id": "abc123", "payload": { "text": "Considering whether to refactor the helper first." } }
```

`agent.output` is intentionally generic. It MAY contain a visible answer chunk,
a structured progress line, or another short model-provided update.

Hidden chain-of-thought MUST NOT be required by this RFC.

### Tool and file activity

- `tool.call.started`
- `tool.call.finished`
- `file.edited`

Examples:

```json
{ "type": "event", "kind": "tool.call.started", "session_id": "abc123", "payload": { "tool": "search", "args": { "query": "AuthError" } } }
{ "type": "event", "kind": "tool.call.finished", "session_id": "abc123", "payload": { "tool": "search", "ok": true } }
{ "type": "event", "kind": "file.edited", "session_id": "abc123", "payload": { "path": "src/auth.ts" } }
```

### Errors

- `error`

Example:

```json
{ "type": "event", "kind": "error", "session_id": "abc123", "payload": { "message": "tool timeout" } }
```

## Commands

The following commands are defined in v0.1.

### Interrupt current work

```json
{ "type": "command", "name": "interrupt", "session_id": "abc123", "request_id": "req-1" }
```

### Send a follow-up user message

```json
{ "type": "command", "name": "send_user_message", "session_id": "abc123", "request_id": "req-2", "payload": { "text": "Stop and explain the change first", "urgent": true } }
```

### Pause or resume the CLI

```json
{ "type": "command", "name": "pause", "session_id": "abc123", "request_id": "req-3" }
{ "type": "command", "name": "resume", "session_id": "abc123", "request_id": "req-4" }
```

### Select the active session

```json
{ "type": "command", "name": "select_session", "session_id": "abc123", "request_id": "req-5" }
```

### Command results

```json
{ "type": "command_result", "name": "interrupt", "session_id": "abc123", "request_id": "req-1", "ok": true }
{ "type": "command_result", "name": "send_user_message", "session_id": "abc123", "request_id": "req-2", "ok": false, "error": "manual delivery required" }
```

## Compliance Levels

### Level 1: Basic companion support

Required:

- `hello`
- `hello_ack`
- `control.set_enabled`
- `control.set_subscriptions`
- `session.started`
- `session.ended`
- `turn.started`
- `turn.ended`
- `user.message`
- `agent.output`

This is enough for basic readable trace and narration.

### Level 2: Interactive control

Adds:

- `interrupt`
- `send_user_message`
- `agent.awaiting_input`
- `select_session`

This is enough for real intervention while the CLI is running.

### Level 3: Rich observability

Adds:

- `tool.call.started`
- `tool.call.finished`
- `file.edited`
- optional `agent.thinking`
- `session.selected`

This is enough for high-quality summarization and multi-session UIs.

## JSON Schema

The normative machine-readable schema for this RFC is:

- [schemas/cli-companion-interface.schema.json](schemas/cli-companion-interface.schema.json)

CLI authors SHOULD validate their outgoing and incoming messages against this
schema during development.

## VS Code Host Guidance

This RFC is designed so a VS Code extension can act as a host UI without
needing to parse raw terminal output whenever a CLI already supports the
interface.

Recommended behavior:

- a status bar toggle such as `Voice Copilot: On`
- a `Follow Active Terminal` mode
- if the active terminal exposes the interface, use it directly
- otherwise fall back to lower-confidence terminal observation mode
- if the user switches terminals, pause narration for the previous active
  session and resume narration for the newly selected one

## Security And Privacy Considerations

- Companion integrations SHOULD be local-only by default
- CLI authors SHOULD make companion mode explicit and user-visible
- hidden reasoning MUST NOT be required by the protocol
- subscriptions SHOULD let the CLI minimize data emission
- commands MUST target a specific session when multiple sessions exist

## Rationale

The contract is intentionally small.

It does not assume:

- a specific UI
- a specific voice provider
- a specific LLM vendor
- a specific transport
- access to raw hidden chain-of-thought

It only assumes that a coding CLI can expose a structured session/event stream,
can optionally accept commands, and can be enabled or disabled by a companion.

That is enough to support Voice Copilot, a VS Code extension, accessibility
tools, dashboards, or other companion products without requiring every CLI to
embed the same UI layer internally.