// Reports this DeepSeek Harness session to the local Voice Copilot companion, and says the
// user's messages to the agent as the user. Nothing leaves this machine here: the companion
// listens on 127.0.0.1, and its token is in a file only this user reads.

import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { request } from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";

const CLI = "dsh";
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

function runtimeDir() {
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
function isSwitchedOff(sessionId) {
  try {
    const word = join(runtimeDir(), "sessions", String(sessionId).replace(/[^\w.-]/g, "_"));
    return readFileSync(word, "ascii") === "off";
  } catch {
    return existsSync(join(runtimeDir(), "switched-off"));
  }
}

function companion() {
  try {
    return JSON.parse(readFileSync(join(runtimeDir(), "companion.json"), "utf-8"));
  } catch {
    return null;
  }
}

// node:http rather than fetch: fetch gives up after 300 s without response headers,
// and a hook held by a pause has to wait for as long as the user takes.
function call(to, method, path, body, signal) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const req = request(
      {
        host: to.host,
        port: to.port,
        path,
        method,
        signal,
        headers: {
          [TOKEN_HEADER]: to.token,
          ...(payload === undefined ? {} : { "Content-Type": "application/json" }),
        },
      },
      (res) => {
        let text = "";
        res.setEncoding("utf-8");
        res.on("data", (chunk) => (text += chunk));
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
function run(file, args, stdin) {
  return new Promise((resolve) => {
    const child = execFile(file, args, { timeout: 60_000, windowsHide: true }, (failure, stdout) =>
      resolve(failure ? null : stdout.trim()),
    );
    child.stdin?.end(stdin);
  });
}

// What `voice-copilot-hook` makes of this message, or null if it is for the agent.
function runChat(text, sessionId) {
  const args = ["--chat", CLI, ...(sessionId ? ["--session", sessionId] : []), "--", text];
  return new Promise((resolve) => {
    execFile("voice-copilot-hook", args, { timeout: 60_000, windowsHide: true }, (failure, stdout) => {
      if (failure?.code === NOT_A_COMMAND_EXIT) resolve(null);
      else if (failure) resolve({ text: `Voice Copilot's companion is missing or out of date. ${INSTALL}` });
      else resolve(JSON.parse(stdout));
    });
  });
}

const textOf = (blocks, type = "text") =>
  blocks.filter((block) => block.type === type).map((block) => block.text ?? "").join("\n").trim();

// What dsh-llm's createUserMessage makes. Written out, so that this file needs no package
// and can be loaded from any path with `dsh --patch`.
const asUser = (text) =>
  Object.freeze({
    id: randomUUID(),
    role: "user",
    content: Object.freeze([Object.freeze({ type: "text", text })]),
    source: Object.freeze({ kind: "user" }),
  });

export const name = "voice-copilot";
export const inject = ["agents"];

export function apply(ctx) {
  if (process.env.VOICE_COPILOT_NO_HOOKS) return;

  // Everyone we report on, by agent id. A sub-agent reports under its root's session and
  // shares its root's line, and only a root gets mail.
  const watched = new Map();
  // Messages we steered in ourselves: their mail announced them already.
  const ourMail = new Set();
  // Switch commands we answered ourselves: for the companion, those never happened.
  const ourCalls = new Set();

  const forward = (who, event, fields = {}, signal) => {
    // One after another: a tool's start must not reach the companion after its end.
    const sent = who.line.queue.then(async () => {
      if (isSwitchedOff(who.sessionId)) return null;
      const to = companion();
      if (to === null) return null;
      const body = {
        hook_event_name: event,
        session_id: who.sessionId,
        ...(who.agentId ? { agent_id: who.agentId } : {}),
        cwd: who.cwd,
        ...fields,
      };
      try {
        const text = await call(to, "POST", `/api/hooks/${CLI}`, body, signal);
        return text ? JSON.parse(text) : null;
      } catch {
        return null; // This reports on the agent, it isn't part of it.
      }
    });
    who.line.queue = sent;
    return sent;
  };

  const takeMail = async (who) => {
    if (isSwitchedOff(who.sessionId)) return [];
    const to = companion();
    if (to === null) return [];
    try {
      const path = `/api/hooks/${CLI}/mail?session_id=${encodeURIComponent(who.sessionId)}`;
      const mail = JSON.parse(await call(to, "GET", path));
      for (const notice of mail.notices ?? []) tell(notice);
      return mail.messages;
    } catch {
      return []; // The companion went away.
    }
  };

  const readMail = async (who) => {
    for (const text of await takeMail(who)) {
      try {
        const message = asUser(text);
        ourMail.add(message.id);
        if (who.agent.status === "idle") who.agent.followup(message);
        else who.agent.steer(message);
      } catch (failure) {
        await forward(who, "PluginError", {
          message: `Couldn't say this to the agent: ${text} (${String(failure)})`,
        });
      }
    }
  };

  const mail = setInterval(() => {
    for (const who of watched.values()) if (!who.agentId) void readMail(who);
  }, MAIL_EVERY_MS);
  mail.unref();

  const watch = (agent) => {
    const { cwd = process.cwd(), origin, parentSession } = agent.session.header;
    const parent = origin === "subagent" ? watched.get(parentSession) : undefined;
    const who = parent
      ? { agent, cwd, sessionId: parent.sessionId, agentId: agent.id, line: parent.line }
      : { agent, cwd, sessionId: agent.id, line: { queue: Promise.resolve() } };
    watched.set(agent.id, who);
    return who;
  };

  const letGo = (who, reason) => {
    watched.delete(who.agent.id);
    return who.agentId ? null : forward(who, "SessionEnd", { reason });
  };

  // A plugin switched on from the Web page finds its sessions running.
  for (const agent of ctx.agents.list()) watch(agent);

  // What the switch says goes to the transcript, outside the model's history, and to
  // stderr, which is all a headless run has.
  const tell = (text) => process.stderr.write(`voice-copilot: ${text}\n`);

  // The session a `/vc` means, which a command handler isn't told.
  let latestRoot;

  // `/vc off` for clients that offer slash commands. `vc off` goes through it as well, as
  // a command's result is the one thing a plugin can show the user in every client.
  ctx.inject(["commands"], (commandsCtx) => {
    commandsCtx.commands.register({
      name: "vc",
      description: "Voice Copilot: on, off, status, check, speed, volume, model and more",
      input: { hint: "[command]" },
      handler: async ({ rawInput }) => {
        const answer = await runChat(`vc ${rawInput}`.trim(), latestRoot);
        if (answer === null) {
          return { kind: "error", text: `Not a Voice Copilot command: vc ${rawInput}. For the list: /vc` };
        }
        const words = answer.text;
        tell(words);
        return { kind: "success", text: words };
      },
    });
  });

  ctx.on("agent/created", async ({ agent, source }) => {
    const who = watch(agent);
    if (!who.agentId) latestRoot = who.sessionId;
    if (who.agentId || isSwitchedOff(who.sessionId)) return;
    // Only a new session may start the companion: its own command does it.
    const started = await run(
      "voice-copilot-hook",
      [CLI],
      JSON.stringify({
        hook_event_name: "SessionStart",
        session_id: who.sessionId,
        cwd: who.cwd,
        source,
        model: agent.options.model,
      }),
    );
    if (started === null) tell(`Voice Copilot's companion isn't installed. ${INSTALL}`);
    else if (started) {
      const said = JSON.parse(started).systemMessage;
      if (said) tell(said);
    }
    await readMail(who); // So that the companion knows this session reads its own.
  });

  ctx.on("agent/disposed", ({ agent }) => {
    const who = watched.get(agent.id);
    if (who) void letGo(who, "disposed");
  });

  ctx.effect(() => async () => {
    clearInterval(mail);
    await Promise.all([...watched.values()].map((who) => letGo(who, "shutdown")));
  }, "voice-copilot: say goodbye");

  // Before the step, so that `vc off` can end the turn before the model hears of it.
  ctx.on(
    "agent/pre-step",
    async ({ agent, messages, turn, step, signal }, next) => {
      const who = watched.get(agent.id);
      if (!who || who.agentId) return next();
      const prompts = [];
      const switches = new Set();
      for (const message of messages) {
        if (message.source.kind !== "user" || ourMail.delete(message.id)) continue;
        const text = textOf(message.content);
        latestRoot = who.sessionId;
        const answer = MAYBE_A_COMMAND.test(text) ? await runChat(text, who.sessionId) : null;
        if (answer === null) {
          prompts.push(text);
          continue;
        }
        switches.add(message.id);
        tell(answer.text);
      }
      if (step === 1 && switches.size > 0 && switches.size === messages.length) {
        // Nothing left to say, so the turn closes without a model request.
        who.silentTurn = turn;
        return { kind: "enter", messages: [] };
      }
      const added = [];
      for (const prompt of prompts) {
        const reply = await forward(
          who,
          "UserPromptSubmit",
          { prompt, ...(step > 1 ? { mid_turn: true } : {}) },
          signal,
        );
        // Mail that was waiting while the agent was paused.
        const waiting = reply?.hookSpecificOutput?.additionalContext;
        if (waiting) added.push(asUser(waiting));
      }
      // The inbox was claimed before this runs, so what the timer steers now misses this
      // step. Anything said by now goes straight in, or a fast model is a step ahead of it.
      for (const text of await takeMail(who)) added.push(asUser(text));
      const decision = await next();
      if (decision.kind !== "enter") return decision;
      return {
        ...decision,
        messages: [...decision.messages.filter((message) => !switches.has(message.id)), ...added],
      };
    },
    { prepend: true },
  );

  ctx.on(
    "tools/pre-execute",
    async (exec, next) => {
      const who = watched.get(exec.agent?.id);
      if (!who) return next();
      const command = typeof exec.arguments?.command === "string" ? exec.arguments.command : "";
      const asked = AGENT_SWITCH.exec(command);
      if (asked !== null) {
        // The command never runs: the agent reads the result as the refusal's reason.
        ourCalls.add(exec.callId);
        const argv = ["switch", asked[1] ?? "status", "--by-agent", "--session", who.sessionId];
        const words = await run("voice-copilot", argv);
        return { kind: "deny", reason: `The plugin carried this out in place of the command, so it did work and there is nothing to retry. ${words ?? `Not installed. ${INSTALL}`}` };
      }
      const reply = await forward(
        who,
        "PreToolUse",
        { tool_name: exec.name, tool_use_id: exec.callId, tool_input: exec.arguments },
        exec.signal, // A stop while held by a pause must not wait for the resume.
      );
      const decided = reply?.hookSpecificOutput;
      if (decided?.permissionDecision === "deny") {
        return { kind: "deny", reason: decided.permissionDecisionReason ?? "Stopped by Voice Copilot." };
      }
      return next();
    },
    { prepend: true },
  );

  ctx.on("tools/result", (exec, result) => {
    const who = watched.get(exec.agent?.id);
    if (!who || ourCalls.delete(exec.callId)) return;
    const output = textOf(result.content);
    void forward(who, result.isError ? "PostToolUseFailure" : "PostToolUse", {
      tool_name: exec.name,
      tool_use_id: exec.callId,
      ...(result.isError ? { error: output } : { tool_response: output }),
    });
  });

  ctx.on("session/event", (session, event) => {
    const who = watched.get(session.id);
    if (!who) return;
    if (event.type === "assistant/message") {
      const parts = event.data.message.content;
      void forward(who, "AssistantMessage", {
        text: textOf(parts),
        thinking: textOf(parts, "reasoning"),
      });
    } else if (event.type === "turn/end" && event.data.turn !== who.silentTurn) {
      const reason = event.data.reason;
      if (reason?.kind === "error") void forward(who, "PluginError", { message: reason.error.message });
      const end = who.agentId ? "SubagentStop" : reason?.kind === "aborted" ? "Interrupt" : "Stop";
      void forward(who, end);
    }
  });
}
