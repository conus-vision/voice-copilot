/**
 * Voice Copilot plugin for OpenCode: your OpenCode session, narrated by voice.
 *
 * Sends what the agent does (your prompt, streamed text and reasoning, tool
 * calls with results, permission prompts) to a running Voice Copilot, which
 * speaks short updates. Voice Copilot can also answer back: hold a tool call
 * while you have the agent paused, stop it after a Supervisor STOP, or pass on
 * a message you said by voice.
 *
 * Install: copy this file to ~/.config/opencode/plugins/ (every project) or
 * .opencode/plugins/ (one project), or run `voice-copilot integrate opencode`.
 *
 * When Voice Copilot is not running every request fails at once and OpenCode
 * works as usual. VOICE_COPILOT_HOOKS=off turns it off without removing it.
 */
import type { Plugin } from "@opencode-ai/plugin";
import http from "node:http";

const BASE = (process.env.VOICE_COPILOT_URL || "http://127.0.0.1:8765/api/companion/v1").replace(/\/+$/, "");
const LAUNCH = process.env.VOICE_COPILOT_LAUNCH || "";
const MODE = process.env.VOICE_COPILOT_MODE || "";
const CLI = "opencode";
const FLUSH_MS = 400;
const GATE_TIMEOUT_MS = 3_600_000;

/** POST JSON over node:http (never through an HTTP proxy). Null when unreachable. */
function post(path: string, body: unknown, timeoutMs = 10_000): Promise<any | null> {
  return new Promise((resolve) => {
    let settled = false;
    let responded = false;
    const done = (value: any | null) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const data = Buffer.from(JSON.stringify(body));
    const req = http.request(
      new URL(BASE + path),
      { method: "POST", headers: { "content-type": "application/json", "content-length": data.length } },
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
    const connectTimer = setTimeout(() => req.destroy(), 2000);
    req.on("socket", (socket) => socket.on("connect", () => clearTimeout(connectTimer)));
    req.setTimeout(timeoutMs, () => req.destroy());
    req.on("error", () => done(null));
    // Bun (OpenCode's runtime) closes the request before the response has
    // ended; only a request that never got a response counts as failed here.
    req.on("close", () => {
      clearTimeout(connectTimer);
      if (!responded) done(null);
    });
    req.end(data);
  });
}

export const VoiceCopilot: Plugin = async ({ client, directory }) => {
  if ((process.env.VOICE_COPILOT_HOOKS || "").toLowerCase() === "off") return {};

  const roles = new Map<string, string>(); // messageID -> role
  const partText = new Map<string, string>(); // partID -> text so far
  const pending = new Map<string, object[]>(); // sessionID -> queued events
  const started = new Set<string>();
  const busy = new Set<string>(); // sessions in the middle of a turn
  let current = "";
  let timer: ReturnType<typeof setTimeout> | undefined;

  const envelope = (sessionID: string) => ({ cli: CLI, session_id: sessionID, launch: LAUNCH, mode: MODE, cwd: directory });

  async function flush() {
    if (timer) clearTimeout(timer);
    timer = undefined;
    const batches = [...pending.entries()];
    pending.clear();
    for (const [sessionID, events] of batches) {
      if (events.length) await post("/events", { ...envelope(sessionID), events });
    }
  }

  function emit(sessionID: string, kind: string, payload: object = {}, now = false) {
    if (!sessionID) return;
    current = sessionID;
    if (!started.has(sessionID)) {
      started.add(sessionID);
      pending.set(sessionID, [...(pending.get(sessionID) ?? []), { kind: "session.started", payload: { cwd: directory } }]);
    }
    const queue = pending.get(sessionID) ?? [];
    const last: any = queue[queue.length - 1];
    // Streamed text arrives in small deltas; merge neighbours into one event.
    if ((kind === "agent.output" || kind === "agent.thinking") && last?.kind === kind) {
      last.payload.text += (payload as any).text;
    } else {
      queue.push({ kind, payload: { ...payload } });
    }
    pending.set(sessionID, queue);
    if (now) void flush();
    else if (!timer) timer = setTimeout(() => void flush(), FLUSH_MS);
  }

  function newText(part: any, delta?: string): string {
    if (typeof delta === "string") {
      partText.set(part.id, (partText.get(part.id) ?? "") + delta);
      return delta;
    }
    const before = partText.get(part.id) ?? "";
    const text = String(part.text ?? "");
    partText.set(part.id, text);
    return text.startsWith(before) ? text.slice(before.length) : text;
  }

  // Commands from Voice Copilot: a voice message, or stop.
  void (async () => {
    for (;;) {
      if (!current) {
        await new Promise((r) => setTimeout(r, 1000)); // no session yet
        continue;
      }
      const cmd = await post("/commands/next", { ...envelope(current), wait_s: 25 }, 35_000);
      if (cmd === null) {
        await new Promise((r) => setTimeout(r, 5000));
        continue;
      }
      if (!cmd.name || !current) continue;
      let ok = true;
      let error: string | undefined;
      try {
        if (cmd.name === "interrupt") {
          await client.session.abort({ path: { id: current } });
        } else if (cmd.name === "send_user_message") {
          await client.session.promptAsync({
            path: { id: current },
            body: { parts: [{ type: "text", text: String(cmd.payload?.text ?? "") }] },
          });
        } else {
          ok = false;
          error = `unsupported command ${cmd.name}`;
        }
      } catch (e) {
        ok = false;
        error = String(e);
      }
      void post("/commands/result", {
        ...envelope(current),
        type: "command_result",
        name: cmd.name,
        request_id: cmd.request_id,
        ok,
        error,
        payload: cmd.payload,
      });
    }
  })();

  return {
    event: async ({ event }) => {
      const e: any = event;
      const p: any = e.properties ?? {};
      switch (e.type) {
        case "message.updated":
          if (p.info?.id) roles.set(p.info.id, p.info.role);
          break;
        case "message.part.updated": {
          const part = p.part;
          if (!part?.sessionID) break;
          const role = roles.get(part.messageID);
          if (part.type === "text" && role === "user" && !part.synthetic) {
            if (!partText.has(part.id)) {
              partText.set(part.id, String(part.text ?? ""));
              emit(part.sessionID, "user.message", { text: String(part.text ?? "") });
            }
          } else if (part.type === "text" && role !== "user") {
            const text = newText(part, p.delta);
            if (text) emit(part.sessionID, "agent.output", { text });
          } else if (part.type === "reasoning") {
            const text = newText(part, p.delta);
            if (text) emit(part.sessionID, "agent.thinking", { text });
          }
          break;
        }
        case "session.status":
          // Reported many times per turn; only the change to busy starts one.
          if (p.status?.type === "busy" && !busy.has(p.sessionID)) {
            busy.add(p.sessionID);
            emit(p.sessionID, "turn.started");
          }
          break;
        case "session.idle":
          busy.delete(p.sessionID);
          emit(p.sessionID, "turn.ended", { final: true }, true);
          break;
        case "permission.updated":
        case "permission.asked":
          emit(p.sessionID, "agent.awaiting_input", { reason: "permission", message: String(p.title ?? "") }, true);
          break;
        case "session.error":
          emit(p.sessionID, "error", { message: String(p.error?.data?.message ?? p.error?.name ?? "error") }, true);
          break;
        case "session.deleted":
          if (p.info?.id) emit(p.info.id, "session.ended", {}, true);
          break;
      }
    },

    "tool.execute.before": async (input, output) => {
      emit(input.sessionID, "tool.call.started", { id: input.callID, tool: input.tool, args: output.args });
      await flush();
      const verdict = await post(
        "/gate",
        { ...envelope(input.sessionID), tool: input.tool, tool_call_id: input.callID },
        GATE_TIMEOUT_MS,
      );
      // A STOP also arrives as an interrupt command, which aborts the run.
      if (verdict?.decision === "deny") throw new Error(String(verdict.reason || "Stopped by Voice Copilot"));
    },

    "tool.execute.after": async (input, output) => {
      const failed = Boolean((output as any)?.metadata?.error);
      emit(input.sessionID, "tool.call.finished", {
        id: input.callID,
        tool: input.tool,
        args: input.args,
        ok: !failed,
        output: String(output?.output ?? "").slice(0, 2000),
      });
    },
  };
};
