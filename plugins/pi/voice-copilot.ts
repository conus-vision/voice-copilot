// Reports this Pi session to the local Voice Copilot companion, and says the user's
// messages to the agent as the user. Nothing leaves this machine here: the companion
// listens on 127.0.0.1, and its token is in a file only this user reads.
//
// The same file runs on oh-my-pi, a fork of Pi whose extension API has drifted a little.

import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { request } from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const IS_OH_MY_PI = process.argv.some((arg) => arg.includes("oh-my-pi"));
const CLI = IS_OH_MY_PI ? "omp" : "pi";
const TOKEN_HEADER = "X-Voice-Copilot-Token";
const MAIL_EVERY_MS = 1000;
const INSTALL = "Install it with `uv tool install voice-copilot`, then start a new session.";

// Might be one of the `vc` words. `voice-copilot-hook --chat` decides, strictly: the whole
// message has to fit, so talking about it is safe.
const MAYBE_A_COMMAND = /^\s*vc(\s|$)/i;
const NOT_A_COMMAND_EXIT = 3;
// Run by the agent when asked in words. Caught here too: under Supervisor+ the agent
// is refused, and that only holds if the command says the agent ran it.
const AGENT_SWITCH = /^\s*voice-copilot\s+switch\s+(on|off|status)\s*;?\s*$/;

// Pi reads `action`, and oh-my-pi `handled`.
const HANDLED = { action: "handled", handled: true } as const;
// oh-my-pi's steer may interrupt the tools in flight. Its aside waits for the step's end, as Pi's steer does.
const WHILE_WORKING = (IS_OH_MY_PI ? { deliverAs: "aside" } : { deliverAs: "steer" }) as { deliverAs: "steer" };
const RESUMED_FROM = new Set(["reload", "resume", "fork"]);

type Companion = { host: string; port: number; token: string };
type Answer = { text: string; choices?: string[]; pick?: string };
type HookReply = {
  hookSpecificOutput?: { permissionDecision?: string; permissionDecisionReason?: string };
  systemMessage?: string;
};

function runtimeDir(): string {
  const override = process.env.VOICE_COPILOT_RUNTIME_DIR;
  if (override) return override;
  if (process.platform === "win32") {
    return join(process.env.LOCALAPPDATA ?? join(homedir(), "AppData", "Local"), "voice-copilot");
  }
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "voice-copilot");
  }
  return join(process.env.XDG_CONFIG_HOME ?? join(homedir(), ".config"), "voice-copilot");
}

// `vc off` leaves a word for this session; `vc off all` turns off whatever has no word.
function isSwitchedOff(sessionId: string): boolean {
  try {
    const word = join(runtimeDir(), "sessions", sessionId.replace(/[^\w.-]/g, "_"));
    return readFileSync(word, "ascii") === "off";
  } catch {
    return existsSync(join(runtimeDir(), "switched-off"));
  }
}

function companion(): Companion | null {
  try {
    return JSON.parse(readFileSync(join(runtimeDir(), "companion.json"), "utf-8")) as Companion;
  } catch {
    return null;
  }
}

// node:http rather than fetch: fetch gives up after 300 s without response headers,
// and a hook held by a pause has to wait for as long as the user takes.
function call(to: Companion, method: string, path: string, body?: unknown): Promise<string> {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const req = request(
      {
        host: to.host,
        port: to.port,
        path,
        method,
        headers: {
          [TOKEN_HEADER]: to.token,
          ...(payload === undefined ? {} : { "Content-Type": "application/json" }),
        },
      },
      (res) => {
        let text = "";
        res.setEncoding("utf-8");
        res.on("data", (chunk: string) => (text += chunk));
        res.on("end", () =>
          (res.statusCode ?? 500) < 400 ? resolve(text) : reject(new Error(`HTTP ${res.statusCode}`)),
        );
      },
    );
    req.on("error", reject);
    req.end(payload);
  });
}

// voice-copilot's own commands own the switch and starting the companion, so there is
// one implementation of each. Answers null when the package isn't installed.
function run(file: string, args: string[], stdin?: string): Promise<string | null> {
  return new Promise((resolve) => {
    const child = execFile(file, args, { timeout: 60_000, windowsHide: true }, (failure, stdout) =>
      resolve(failure ? null : stdout.trim()),
    );
    child.stdin?.end(stdin);
  });
}

// What `voice-copilot-hook` makes of this message, or null if it is for the agent.
function runChat(text: string, sessionId: string): Promise<Answer | null> {
  const args = ["--chat", CLI, "--session", sessionId, "--", text];
  return new Promise((resolve) => {
    execFile("voice-copilot-hook", args, { timeout: 60_000, windowsHide: true }, (failure, stdout) => {
      if ((failure as { code?: unknown } | null)?.code === NOT_A_COMMAND_EXIT) resolve(null);
      else if (failure) resolve({ text: `Voice Copilot's companion is missing or out of date. ${INSTALL}` });
      else resolve(JSON.parse(stdout) as Answer);
    });
  });
}

export default function (pi: ExtensionAPI) {
  if (process.env.VOICE_COPILOT_NO_HOOKS) return;

  let sessionId = "";
  let mailTimer: ReturnType<typeof setInterval> | undefined;
  // One after another: a tool's start must not reach the companion after its end.
  let queue: Promise<unknown> = Promise.resolve();

  const send = async (event: string, fields: Record<string, unknown> = {}): Promise<HookReply | null> => {
    if (isSwitchedOff(sessionId)) return null;
    const to = companion();
    if (to === null) return null;
    const body = { hook_event_name: event, session_id: sessionId, cwd: process.cwd(), ...fields };
    try {
      const text = await call(to, "POST", `/api/hooks/${CLI}`, body);
      return text ? (JSON.parse(text) as HookReply) : null;
    } catch {
      return null; // This reports on the agent, it isn't part of it.
    }
  };

  const forward = (event: string, fields: Record<string, unknown> = {}): Promise<HookReply | null> => {
    const sent = queue.then(() => send(event, fields));
    queue = sent;
    return sent;
  };

  const readMail = async (ctx: ExtensionContext): Promise<void> => {
    if (isSwitchedOff(sessionId)) return;
    const to = companion();
    if (to === null) return;
    let messages: string[];
    try {
      const path = `/api/hooks/${CLI}/mail?session_id=${encodeURIComponent(sessionId)}`;
      const mail = JSON.parse(await call(to, "GET", path)) as { messages: string[]; notices?: string[] };
      messages = mail.messages;
      for (const notice of mail.notices ?? []) ctx.ui.notify(notice, "info");
    } catch {
      return; // The companion went away.
    }
    for (const text of messages) {
      try {
        if (ctx.isIdle()) pi.sendUserMessage(text);
        else pi.sendUserMessage(text, WHILE_WORKING);
      } catch (failure) {
        await forward("PluginError", {
          message: `Couldn't say this to the agent: ${text} (${String(failure)})`,
        });
      }
    }
  };

  const show = async (answer: Answer, ctx: ExtensionContext): Promise<void> => {
    ctx.ui.notify(answer.text, "info");
    if (!answer.choices?.length || !answer.pick || !ctx.hasUI) return;
    const chosen = await ctx.ui.select("Voice Copilot", answer.choices);
    if (chosen === undefined) return;
    const done = await runChat(`${answer.pick} ${chosen}`, sessionId);
    if (done) ctx.ui.notify(done.text, "info");
  };

  pi.registerCommand("vc", {
    description: "Voice Copilot: on, off, status, check, speed, volume, model and more",
    handler: async (args, ctx) => {
      const answer = await runChat(`vc ${args}`.trim(), sessionId);
      if (answer) await show(answer, ctx);
      else ctx.ui.notify(`Not a Voice Copilot command: vc ${args}. For the list: /vc`, "warning");
    },
  });

  pi.on("session_start", async (event, ctx) => {
    sessionId = ctx.sessionManager.getSessionId();
    clearInterval(mailTimer);
    mailTimer = setInterval(() => void readMail(ctx), MAIL_EVERY_MS);
    mailTimer.unref();
    if (isSwitchedOff(sessionId)) return;
    // Only a new session may start the companion: its own command does it.
    const started = await run(
      "voice-copilot-hook",
      [CLI],
      JSON.stringify({
        hook_event_name: "SessionStart",
        session_id: sessionId,
        cwd: process.cwd(),
        source: RESUMED_FROM.has(event.reason) ? "resume" : "startup",
        model: ctx.model?.id,
      }),
    );
    if (started === null) ctx.ui.notify(`Voice Copilot's companion isn't installed. ${INSTALL}`, "warning");
    else if (started) {
      const said = (JSON.parse(started) as HookReply).systemMessage;
      if (said) ctx.ui.notify(said, "info");
    }
  });

  pi.on("session_shutdown", async (event) => {
    clearInterval(mailTimer);
    // Past the queue: a tool call held by a pause must not keep Pi from quitting.
    await send("SessionEnd", { reason: event.reason });
  });

  pi.on("input", async (event, ctx) => {
    if (MAYBE_A_COMMAND.test(event.text)) {
      const answer = await runChat(event.text, sessionId);
      if (answer !== null) {
        void show(answer, ctx);
        return HANDLED;
      }
    }
    // Typed to a working agent. What we said ourselves came from mail that announced it.
    if (event.source !== "extension" && !ctx.isIdle()) {
      await forward("UserPromptSubmit", { prompt: event.text, mid_turn: true });
    }
    return { action: "continue" };
  });

  // Rather than `input`, which oh-my-pi raises neither under `-p` nor for our own messages.
  pi.on("before_agent_start", async (event) => {
    if ("prompt" in event) await forward("UserPromptSubmit", { prompt: event.prompt });
  });

  pi.on("tool_call", async (event, ctx) => {
    // A step can take under a second, faster than the timer: said now, it goes with this one.
    await readMail(ctx);
    const input = event.input as Record<string, unknown>;
    const command = typeof input.command === "string" ? input.command : "";
    const asked = AGENT_SWITCH.exec(command);
    if (asked !== null) {
      // The command never runs: the agent reads the result as the refusal's reason.
      const argv = ["switch", asked[1] ?? "status", "--by-agent", "--session", sessionId];
      const words = await run("voice-copilot", argv);
      return { block: true, reason: `The plugin carried this out in place of the command, so it did work and there is nothing to retry. ${words ?? `Not installed. ${INSTALL}`}` };
    }
    const reply = await forward("PreToolUse", {
      tool_name: event.toolName,
      tool_use_id: event.toolCallId,
      tool_input: event.input,
    });
    const decided = reply?.hookSpecificOutput;
    if (decided?.permissionDecision === "deny") {
      return { block: true, reason: decided.permissionDecisionReason ?? "Stopped by Voice Copilot." };
    }
    return undefined;
  });

  pi.on("tool_execution_end", async (event) => {
    const content: { type: string; text?: string }[] = event.result?.content ?? [];
    const output = content.map((part) => part.text ?? "").join("\n");
    await forward(event.isError ? "PostToolUseFailure" : "PostToolUse", {
      tool_name: event.toolName,
      tool_use_id: event.toolCallId,
      ...(event.isError ? { error: output } : { tool_response: output }),
    });
  });

  pi.on("message_end", async (event) => {
    if (event.message.role !== "assistant") return;
    const parts = event.message.content as { type: string; text?: string; thinking?: string }[];
    const pick = (type: string, key: "text" | "thinking") =>
      parts.filter((part) => part.type === type).map((part) => part[key] ?? "").join("\n").trim();
    await forward("AssistantMessage", { text: pick("text", "text"), thinking: pick("thinking", "thinking") });
  });

  pi.on("agent_settled", async (event) => {
    await forward(event.aborted ? "Interrupt" : "Stop");
  });

  if (IS_OH_MY_PI) {
    // Not in Pi's types. It has `agent_settled` for this, and no way to switch session unannounced.
    const on = pi.on.bind(pi) as unknown as (
      event: string,
      handler: (event: unknown, ctx: ExtensionContext) => unknown,
    ) => void;
    on("session_stop", () => forward("Stop"));
    on("session_switch", (_event, ctx) => {
      sessionId = ctx.sessionManager.getSessionId();
    });
  }
}
