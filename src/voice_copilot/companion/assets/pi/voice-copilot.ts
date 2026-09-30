/**
 * Voice Copilot extension for Pi: your Pi session, narrated by voice.
 *
 * Sends what the agent does (your prompt, streamed text and thinking, tool
 * calls and results) to a running Voice Copilot, which speaks short updates.
 * Voice Copilot can also answer back: hold a tool call while you have the
 * agent paused, stop it after a Supervisor STOP, or pass on a message you
 * said by voice.
 *
 * Load it for one session:   pi -e /path/to/voice-copilot.ts
 * Or for every session:      voice-copilot integrate pi
 *                            (copies this file to ~/.pi/agent/extensions/)
 * `voice-copilot vc pi` loads it for you.
 *
 * When Voice Copilot is not running every request fails at once and Pi works
 * as usual. VOICE_COPILOT_HOOKS=off turns it off without removing it.
 * Environment (set by a Voice Copilot launch): VOICE_COPILOT_URL,
 * VOICE_COPILOT_LAUNCH, VOICE_COPILOT_MODE.
 *
 * No dependencies: node:http only, so an HTTP(S)_PROXY setting can never
 * route these loopback calls elsewhere.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import http from "node:http";

const BASE = (process.env.VOICE_COPILOT_URL || "http://127.0.0.1:8765/api/companion/v1").replace(/\/+$/, "");
const LAUNCH = process.env.VOICE_COPILOT_LAUNCH || "";
const MODE = process.env.VOICE_COPILOT_MODE || "";
const TOKEN = process.env.VOICE_COPILOT_TOKEN || "";
const CLI = "pi";
const FLUSH_MS = 400;
const CONNECT_TIMEOUT_MS = 2000;
const GATE_TIMEOUT_MS = 3_600_000;

/** POST JSON to Voice Copilot. Resolves null when it is unreachable. */
function post(path: string, body: unknown, timeoutMs = 10_000, signal?: AbortSignal): Promise<any | null> {
  return new Promise((resolve) => {
    let settled = false;
    let responded = false;
    const done = (value: any | null) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let url: URL;
    try {
      url = new URL(BASE + path);
    } catch {
      done(null);
      return;
    }
    const data = Buffer.from(JSON.stringify(body));
    const req = http.request(
      url,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": data.length,
          ...(TOKEN ? { "x-voice-copilot-token": TOKEN } : {}),
        },
      },
      (res) => {
        responded = true;
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          if ((res.statusCode ?? 500) >= 300) return done(null);
          const text = Buffer.concat(chunks).toString("utf8");
          try {
            done(text ? JSON.parse(text) : {});
          } catch {
            done({});
          }
        });
        res.on("error", () => done(null));
      },
    );
    const connectTimer = setTimeout(() => req.destroy(), CONNECT_TIMEOUT_MS);
    req.on("socket", (socket) => socket.on("connect", () => clearTimeout(connectTimer)));
    req.setTimeout(timeoutMs, () => req.destroy());
    req.on("error", () => done(null));
    // Bun (OpenCode's runtime) closes the request before the response has
    // ended; only a request that never got a response counts as failed here.
    req.on("close", () => {
      clearTimeout(connectTimer);
      if (!responded) done(null);
    });
    signal?.addEventListener("abort", () => req.destroy(), { once: true });
    req.end(data);
  });
}

function summarize(result: any): string {
  if (result == null) return "";
  const content = Array.isArray(result?.content) ? result.content : undefined;
  if (content) {
    return content
      .filter((c: any) => c?.type === "text")
      .map((c: any) => String(c.text ?? ""))
      .join("\n")
      .slice(0, 2000);
  }
  return (typeof result === "string" ? result : JSON.stringify(result)).slice(0, 2000);
}

export default function voiceCopilot(pi: ExtensionAPI) {
  if ((process.env.VOICE_COPILOT_HOOKS || "").toLowerCase() === "off") return;

  let sessionId = "";
  let cwd = "";
  let live: ExtensionContext | undefined;
  let queue: object[] = [];
  let text = "";
  let thinking = "";
  let timer: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;
  const toolArgs = new Map<string, unknown>();

  const envelope = () => ({ cli: CLI, session_id: sessionId, launch: LAUNCH, mode: MODE, cwd });

  function takeDeltas() {
    if (thinking) queue.push({ kind: "agent.thinking", payload: { text: thinking } });
    if (text) queue.push({ kind: "agent.output", payload: { text } });
    thinking = "";
    text = "";
  }

  async function flush() {
    if (timer) clearTimeout(timer);
    timer = undefined;
    takeDeltas();
    if (!queue.length || !sessionId) return;
    const events = queue;
    queue = [];
    await post("/events", { ...envelope(), events });
  }

  function schedule() {
    if (!timer) timer = setTimeout(() => void flush(), FLUSH_MS);
  }

  function emit(kind: string, payload: object = {}) {
    takeDeltas();
    queue.push({ kind, payload });
    schedule();
  }

  async function commandLoop(gen: number) {
    while (gen === generation) {
      const cmd = await post("/commands/next", { ...envelope(), wait_s: 25 }, 35_000);
      if (gen !== generation) return;
      if (cmd === null) {
        await new Promise((r) => setTimeout(r, 5000)); // Voice Copilot is not running
        continue;
      }
      if (!cmd.name) continue;
      let ok = true;
      let error: string | undefined;
      try {
        if (cmd.name === "interrupt") {
          live?.abort();
        } else if (cmd.name === "send_user_message") {
          const message = String(cmd.payload?.text ?? "");
          if (!live || live.isIdle()) pi.sendUserMessage(message);
          else pi.sendUserMessage(message, { deliverAs: "steer" });
        } else {
          ok = false;
          error = `unsupported command ${cmd.name}`;
        }
      } catch (e) {
        ok = false;
        error = String(e);
      }
      void post("/commands/result", {
        ...envelope(),
        type: "command_result",
        name: cmd.name,
        request_id: cmd.request_id,
        ok,
        error,
        payload: cmd.payload,
      });
    }
  }

  pi.on("session_start", async (event, ctx) => {
    live = ctx;
    sessionId = ctx.sessionManager.getSessionId();
    cwd = ctx.cwd;
    emit("session.started", { cwd, reason: event.reason });
    await flush();
    generation += 1;
    void commandLoop(generation);
  });

  pi.on("session_shutdown", async (event) => {
    generation += 1; // stops this session's command loop
    emit("session.ended", { reason: event.reason });
    await flush();
  });

  pi.on("input", async (event) => {
    // Messages this extension delivered are reported by Voice Copilot itself.
    if (event.source !== "extension") emit("user.message", { text: event.text });
    return { action: "continue" };
  });

  pi.on("agent_start", async () => emit("turn.started"));
  pi.on("agent_settled", async () => {
    emit("turn.ended", { final: true });
    await flush();
  });

  pi.on("message_update", async (event) => {
    const update: any = event.assistantMessageEvent;
    if (update?.type === "text_delta" && update.delta) {
      text += update.delta;
      schedule();
    } else if (update?.type === "thinking_delta" && update.delta) {
      thinking += update.delta;
      schedule();
    }
  });

  pi.on("message_end", async (event) => {
    const message: any = event.message;
    if (message?.role === "assistant" && message?.stopReason === "error") {
      emit("error", { message: String(message.errorMessage ?? "model error") });
    }
  });

  pi.on("tool_execution_start", async (event) => {
    toolArgs.set(event.toolCallId, event.args);
    emit("tool.call.started", { id: event.toolCallId, tool: event.toolName, args: event.args });
    await flush();
  });

  pi.on("tool_execution_end", async (event) => {
    const args = toolArgs.get(event.toolCallId);
    toolArgs.delete(event.toolCallId);
    emit("tool.call.finished", {
      id: event.toolCallId,
      tool: event.toolName,
      args,
      ok: !event.isError,
      output: summarize(event.result),
    });
  });

  pi.on("ui_prompt_start", async (event) => {
    emit("agent.awaiting_input", { reason: "prompt", message: event.title ?? "" });
    await flush();
  });

  // Pause and Supervisor STOP: Voice Copilot holds this call or refuses it.
  pi.on("tool_call", async (event, ctx) => {
    await flush();
    const verdict = await post(
      "/gate",
      { ...envelope(), tool: event.toolName, tool_call_id: event.toolCallId },
      GATE_TIMEOUT_MS,
      ctx.signal,
    );
    if (verdict?.decision !== "deny") return undefined;
    if (verdict.stop) setTimeout(() => live?.abort(), 0);
    return { block: true, reason: String(verdict.reason || "Stopped by Voice Copilot") };
  });
}
