# Providers

All of TTS, STT and commentator LLM are loaded by name from a registry. To add
a new backend, drop a module under `voice_copilot/providers/<kind>/`,
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

| Name            | Install   | Notes                                                  |
| ---             | ---       | ---                                                    |
| `anthropic`     | default   | `ANTHROPIC_API_KEY`. Default: `claude-haiku-4-5-20251001`. |
| `openai`        | default   | `OPENAI_API_KEY`. Default: `gpt-4o-mini`.              |
| `openai-compat` | default   | Points at `OPENAI_COMPAT_BASE_URL` (Ollama/LM Studio). |

## Secrets

Keys are read in this order:

1. Process environment (`.env` or shell exports).
2. OS keychain via `keyring` under service `voice-copilot`.
3. Unset → provider constructs without a key; will fail on first call.

Write keys through the settings page (`/settings`) — values never leave the
server back to the browser, only `{name: is_set}` flags.

## Testing a provider

`POST /api/providers/test {kind, name, options}` runs a cheap probe:

- **llm** — streams `"ping"` → returns the first delta.
- **tts** — synthesises `"ok"` → returns the byte count.
- **stt** — constructs the provider (no audio probe yet).

The settings page has a **Test** button per provider that uses this endpoint.

## Proxy routes (external CLIs)

When you run `voice-copilot proxy` (or any command with `--proxy`), the
reverse-proxy exposes these paths for external CLIs to point at via
`*_BASE_URL` env vars:

| Proxy path     | Upstream                                        | Parser      |
| ---            | ---                                             | ---         |
| `/anthropic/*` | `api.anthropic.com`                             | Anthropic SSE |
| `/openai/*`    | `api.openai.com`                                | OpenAI SSE  |
| `/openrouter/*`| `openrouter.ai/api`                             | OpenAI SSE  |
| `/groq/*`      | `api.groq.com/openai`                           | OpenAI SSE  |
| `/mistral/*`   | `api.mistral.ai`                                | OpenAI SSE  |
| `/ollama/*`    | `127.0.0.1:11434`                               | OpenAI SSE (on `/v1/*`) |
| `/gemini/*`    | `generativelanguage.googleapis.com`             | _pass-through_ |

Gemini's stream format is not OpenAI/Anthropic-shaped, so today we forward
the bytes without narration — it still works for your CLI, you just won't
hear what it's doing. A Gemini parser can land as a separate change.

### Sessions

Each distinct `(user-agent, authorization-prefix)` tuple becomes one
**session**. The popup shows a dropdown in the header letting you pick
which session to narrate; events from non-active sessions stay silent (they
still appear in the feed — we don't drop them, just skip TTS). Sessions
live in memory for the lifetime of the proxy process.
