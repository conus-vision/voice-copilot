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
