# Providers

All of TTS, STT and commentator LLM are loaded by name from a registry. To add
a new backend, drop a module under `src/voice_copilot/providers/<kind>/`,
decorate the class with `@register("<kind>", "<name>")`, and add a side-effect
import to the package `__init__.py`.

## TTS

| Name          | Install              | Format | Notes                                    |
| ---           | ---                  | ---    | ---                                      |
| `edge-tts`    | default              | mp3    | Free Microsoft Azure voice. Needs Internet. |
| `silero`      | `[local-tts]`        | wav    | Local, PyTorch. ~100 MB model on first run. |
| `piper`       | `[local-tts]`        | wav    | Local ONNX, very fast on CPU.            |
| `openai`      | default              | mp3    | `OPENAI_API_KEY`. `gpt-4o-mini-tts`.     |
| `elevenlabs`  | `[elevenlabs]`       | mp3    | `ELEVENLABS_API_KEY`.                    |

## STT

| Name                 | Install        | Notes                                             |
| ---                  | ---            | ---                                               |
| `openai-whisper-api` | default        | `OPENAI_API_KEY`. Default — free-tier OK.         |
| `faster-whisper`     | `[local-stt]`  | Local CTranslate2 build. Works fully offline.     |
| `deepgram`           | `[deepgram]`   | `DEEPGRAM_API_KEY`. Cheapest cloud option.        |

## Commentator LLM

The commentator has two modes (`commentator.mode`):

- `auto` (default) — narrate through the **same CLI you launched with `vc`**,
  using its own login. No extra keys. The `auto` provider shells out to the CLI
  with a per-CLI narration profile (`commentator/cli_profiles.py`); with
  `auto_tier_models: true` it picks the weakest model of that CLI for the
  narrator and the strongest for the Supervisor.
- `api` — call one of the API providers below, configured under
  `commentator.provider`. Also what `voice-copilot serve` / `run` use when no
  CLI is being wrapped.

| Name             | Install   | Notes                                                              |
| ---              | ---       | ---                                                                |
| `auto`           | default   | Reuses the launched CLI (Claude Code, Codex, Copilot CLI, …). No key. |
| `anthropic`      | default   | `ANTHROPIC_API_KEY`. Default: `claude-haiku-4-5-20251001`.         |
| `openai`         | default   | `OPENAI_API_KEY`. Default: `gpt-4o-mini`.                          |
| `openai-compat`  | default   | Any OpenAI-shaped server at `OPENAI_COMPAT_BASE_URL` (Ollama, LM Studio; default `http://127.0.0.1:11434/v1`). Optional `OPENAI_COMPAT_API_KEY`. Default model: `llama3.1`. Pick a non-reasoning model — reasoning models put the text in `reasoning` and leave `content` empty. |
| `github-copilot` | default   | Copilot's OpenAI-compatible endpoint. Token from `GITHUB_COPILOT_TOKEN` (keychain or env), `gh auth token`, or the local Copilot `hosts.json`. Default: `gpt-4.1-mini`. |
| `copilot-cli`    | default   | Shells out to the `copilot` binary (GitHub Copilot CLI), which manages its own auth. Default: `gpt-5-mini`. Superseded by `auto` for `vc copilot`. |

Per-CLI overrides live under `commentator.per_cli.<cli>` (mode, provider,
supervisor mode). See [supervisor.md](supervisor.md) for the Supervisor's own
model and modes.

## Secrets

Keys are read in this order:

1. Process environment — shell exports, or a `.env` next to where you run
   `voice-copilot` (loaded at startup, never overriding real exports; see
   `.env.example`).
2. OS keychain via `keyring` under service `voice-copilot`.
3. Unset → provider constructs without a key; will fail on first call.

Known names: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`,
`ELEVENLABS_API_KEY`, `OPENAI_COMPAT_API_KEY`, `GITHUB_COPILOT_TOKEN`.

Write keys through the settings page — values never leave the server back to
the browser, only `{name: is_set}` flags.

## Testing a provider

`POST /api/providers/test {kind, name, options}` runs a cheap probe:

- **llm** — streams `"ping"` → returns the first delta.
- **tts** — synthesises `"ok"` → returns the byte count.
- **stt** — constructs the provider (no audio probe yet).

The settings page has a **Test** button per provider that uses this endpoint.

## Proxy routes (external CLIs)

When you run `voice-copilot proxy` (or any command with `--proxy`, or `vc`),
the reverse-proxy exposes these paths for external CLIs to point at via
`*_BASE_URL` env vars:

| Proxy path         | Env var                    | Upstream                            | Parser        |
| ---                | ---                        | ---                                 | ---           |
| `/anthropic/*`     | `ANTHROPIC_BASE_URL`       | `api.anthropic.com`                 | Anthropic SSE |
| `/openai/*`        | `OPENAI_BASE_URL`          | `api.openai.com`                    | OpenAI SSE    |
| `/openai-chatgpt/*`| `OPENAI_CHATGPT_BASE_URL`  | `chatgpt.com/backend-api/codex`     | OpenAI SSE (Codex on a ChatGPT plan) |
| `/openrouter/*`    | `OPENROUTER_BASE_URL`      | `openrouter.ai/api`                 | OpenAI SSE    |
| `/groq/*`          | `GROQ_BASE_URL`            | `api.groq.com/openai`               | OpenAI SSE    |
| `/mistral/*`       | `MISTRAL_BASE_URL`         | `api.mistral.ai`                    | OpenAI SSE    |
| `/deepseek/*`      | `DEEPSEEK_BASE_URL`        | `api.deepseek.com`                  | OpenAI SSE    |
| `/ollama/*`        | `OLLAMA_BASE_URL`          | `127.0.0.1:11434`                   | OpenAI SSE on `/v1/*`, native NDJSON on `/api/chat` |
| `/opencode-zen/*`  | `OPENCODE_ZEN_BASE_URL`    | `opencode.ai/zen/v1`                | OpenCode Zen  |
| `/gemini/*`        | `GEMINI_BASE_URL`          | `generativelanguage.googleapis.com` | _pass-through_ |

Only `api.anthropic.com` is intercepted on the Anthropic side: Claude Code via
Bedrock, Vertex or Azure Foundry uses other base-URL env vars and is not
narrated.

Gemini's stream format is not OpenAI/Anthropic-shaped, so today we forward
the bytes without narration — it still works for your CLI, you just won't
hear what it's doing. A Gemini parser can land as a separate change.

### Sessions

Each distinct `(user-agent, authorization-prefix)` tuple becomes one
**session**. The popup shows a dropdown in the header letting you pick
which session to narrate; events from non-active sessions stay silent (they
still appear in the feed — we don't drop them, just skip TTS). Sessions
live in memory for the lifetime of the proxy process.
