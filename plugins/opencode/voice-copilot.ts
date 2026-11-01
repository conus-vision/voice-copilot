// Reports this OpenCode instance's sessions to the local Voice Copilot companion, and says
// the user's messages to the agent as the user. Nothing leaves this machine here: the
// companion listens on 127.0.0.1, and its token is in a file only this user reads.

import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { request } from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Hooks, PluginInput } from "@opencode-ai/plugin";

const CLI = "opencode";
const TOKEN_HEADER = "X-Voice-Copilot-Token";
const MAIL_EVERY_MS = 1000;
const TOAST_AFTER_MS = 500;
const INSTALL = "Install it with `uv tool install voice-copilot`, then start a new session.";

// Might be one of the `vc` words. `voice-copilot-hook --chat` decides, strictly: the whole
// message has to fit, so talking about it is safe.
// `opencode run "vc off"` keeps the quotes: it quotes any argument with a space in it.
const MAYBE_A_COMMAND = /^\s*"?\s*vc(\s|"|$)/i;
const NOT_A_COMMAND_EXIT = 3;
// Run by the agent when asked in words. Caught here too: under Supervisor+ the agent
// is refused, and that only holds if the command says the agent ran it.
const AGENT_SWITCH = /^\s*voice-copilot\s+switch\s+(on|off|status)\s*;?\s*$/;
// What we throw to stop the model, which OpenCode may report back as the session's error.
const HANDLED = "Handled by Voice Copilot, the model wasn't called.";

type Companion = { host: string; port: number; token: string };
type HookReply = {
  hookSpecificOutput?: { permissionDecision?: string; permissionDecisionReason?: string };
  systemMessage?: string;
};
// What a message said on the user's behalf has to carry, or the session changes agent.
type Target = { agent: string; model: { providerID: string; modelID: string }; variant?: string };

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
function isSwitchedOff(sessionId?: string): boolean {
  try {
    if (sessionId) {
      const word = join(runtimeDir(), "sessions", sessionId.replace(/[^\w.-]/g, "_"));
      return readFileSync(word, "ascii") === "off";
    }
  } catch {
    // No word about this session in particular.
  }
  return existsSync(join(runtimeDir(), "switched-off"));
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
function runChat(text: string, sessionId: string): Promise<{ text: string } | null> {
  const unquoted = text.trim().replace(/^"|"$/g, "");
  const args = ["--chat", CLI, "--session", sessionId, "--", unquoted];
  return new Promise((resolve) => {
    execFile("voice-copilot-hook", args, { timeout: 60_000, windowsHide: true }, (failure, stdout) => {
      if ((failure as { code?: unknown } | null)?.code === NOT_A_COMMAND_EXIT) resolve(null);
      else if (failure) resolve({ text: `Voice Copilot's companion is missing or out of date. ${INSTALL}` });
      else resolve(JSON.parse(stdout) as { text: string });
    });
  });
}

export default async function ({
  client,
  directory,
}: PluginInput): Promise<Hooks & { dispose?: () => Promise<void> }> {
  if (process.env.VOICE_COPILOT_NO_HOOKS) return {};

  const targets = new Map<string, Target>(); // The sessions the user talks to.
  const parents = new Map<string, string>(); // A subagent's session, and whose it is.
  const fresh = new Set<string>(); // Created since we loaded, so not a resumed session.
  const started = new Set<string>();
  const busy = new Set<string>();
  const aborted = new Set<string>();
  const reported = new Set<string>(); // Pruning old tool output sends finished parts again.
  const steered = new Set<string>(); // Mail we are saying, on its way back to chat.message.
  // One after another: a tool's start must not reach the companion after its end.
  const queues = new Map<string, Promise<unknown>>();

  const rootOf = (id: string): string => {
    const parent = parents.get(id);
    return parent === undefined ? id : rootOf(parent);
  };

  const forward = (
    id: string,
    event: string,
    fields: Record<string, unknown> = {},
  ): Promise<HookReply | null> => {
    const root = rootOf(id);
    const body = {
      hook_event_name: event,
      session_id: root,
      cwd: directory,
      ...(root === id ? {} : { agent_id: id }),
      ...fields,
    };
    const sent = (queues.get(root) ?? Promise.resolve()).then(async () => {
      if (isSwitchedOff(root)) return null;
      const to = companion();
      if (to === null) return null;
      try {
        const text = await call(to, "POST", `/api/hooks/${CLI}`, body);
        return text ? (JSON.parse(text) as HookReply) : null;
      } catch {
        return null; // This reports on the agent, it isn't part of it.
      }
    });
    queues.set(root, sent);
    return sent;
  };

  // A toast in the terminal UI, late enough to replace the "Failed to send prompt" one it
  // draws for a prompt we stopped. `opencode run` draws none, but its stderr is free.
  const tell = (message: string, variant: "info" | "warning"): void => {
    if (process.argv.includes("run")) process.stderr.write(`${message}\n`);
    const toast = () => void client.tui.showToast({ body: { message, variant } }).catch(() => undefined);
    setTimeout(toast, TOAST_AFTER_MS).unref();
  };

  const readMail = async (): Promise<void> => {
    if (targets.size === 0) return;
    const to = companion();
    if (to === null) return;
    for (const [id, target] of targets) {
      if (isSwitchedOff(id)) continue;
      let messages: string[];
      try {
        const path = `/api/hooks/${CLI}/mail?session_id=${encodeURIComponent(id)}`;
        const mail = JSON.parse(await call(to, "GET", path)) as { messages: string[]; notices?: string[] };
        messages = mail.messages;
        for (const notice of mail.notices ?? []) tell(notice, "info");
      } catch {
        return; // The companion went away.
      }
      for (const text of messages) {
        // The same call for a working session and an idle one: the first takes it at its
        // next step, the second starts a turn.
        steered.add(text);
        const sent = await client.session
          .promptAsync({ path: { id }, body: { ...target, parts: [{ type: "text", text }] } })
          .catch((failure: unknown) => ({ error: failure }));
        if (sent.error) {
          steered.delete(text);
          await forward(id, "PluginError", {
            message: `Couldn't say this to the agent: ${text} (${JSON.stringify(sent.error)})`,
          });
        }
      }
    }
  };

  const mailTimer = setInterval(() => void readMail(), MAIL_EVERY_MS);
  mailTimer.unref();

  // Only a new session may start the companion: its own command does it.
  const start = async (id: string, model: string): Promise<void> => {
    if (started.has(id) || isSwitchedOff(id)) return;
    started.add(id);
    const said = await run(
      "voice-copilot-hook",
      [CLI],
      JSON.stringify({
        hook_event_name: "SessionStart",
        session_id: id,
        cwd: directory,
        source: fresh.has(id) ? "startup" : "resume",
        model,
      }),
    );
    if (said === null) tell(`Voice Copilot's companion isn't installed. ${INSTALL}`, "warning");
    else if (said) {
      const greeting = (JSON.parse(said) as HookReply).systemMessage;
      if (greeting) tell(greeting, "info");
    }
  };

  return {
    // Nothing in OpenCode ends a session, only its process or its instance.
    dispose: async () => {
      clearInterval(mailTimer);
      await Promise.all([...targets.keys()].map((id) => forward(id, "SessionEnd", { reason: "exit" })));
    },

    // Not awaited by OpenCode, and a failure here would go unhandled: `forward` can't fail.
    event: async ({ event }) => {
      if (event.type === "session.created") {
        const { id, parentID } = event.properties.info;
        if (parentID) parents.set(id, parentID);
        else fresh.add(id);
      } else if (event.type === "session.deleted") {
        const { id } = event.properties.info;
        if (targets.delete(id)) await forward(id, "SessionEnd", { reason: "deleted" });
      } else if (event.type === "session.status") {
        const { sessionID: id, status } = event.properties;
        if (status.type === "busy") busy.add(id);
        // An aborted turn goes idle twice.
        if (status.type !== "idle" || !busy.delete(id)) return;
        const wasAborted = aborted.delete(id);
        await forward(id, parents.has(id) ? "SubagentStop" : wasAborted ? "Interrupt" : "Stop");
      } else if (event.type === "session.error") {
        const { sessionID: id, error } = event.properties;
        if (!id || !error) return;
        if (error.name === "MessageAbortedError") {
          aborted.add(id);
          return;
        }
        const message = (error as { data?: { message?: string } }).data?.message ?? error.name;
        if (message.includes(HANDLED)) return;
        await forward(id, "PluginError", { message: message.split("\n")[0] });
      } else if (event.type === "message.part.updated") {
        const { part } = event.properties;
        if (reported.has(part.id)) return;
        if (part.type === "tool" && (part.state.status === "completed" || part.state.status === "error")) {
          // Here rather than `tool.execute.after`, which a failed tool never reaches.
          reported.add(part.id);
          const { state } = part;
          await forward(part.sessionID, state.status === "error" ? "PostToolUseFailure" : "PostToolUse", {
            tool_name: part.tool,
            tool_use_id: part.callID,
            ...(state.status === "error" ? { error: state.error } : { tool_response: state.output }),
          });
        } else if ((part.type === "text" || part.type === "reasoning") && part.time?.end) {
          // Only the model's parts have an end, and it comes before any tool it then calls.
          reported.add(part.id);
          const said = part.type === "text" ? { text: part.text } : { thinking: part.text };
          await forward(part.sessionID, "AssistantMessage", said);
        }
      }
    },

    "chat.message": async (input, output) => {
      // Synthetic parts are OpenCode's own, such as the contents of an attached file.
      const typed = output.parts.map((part) => (part.type === "text" && !part.synthetic ? part.text : ""));
      const text = typed.join("");
      const id = input.sessionID;
      const answer = MAYBE_A_COMMAND.test(text) ? await runChat(text, id) : null;
      if (answer !== null) {
        tell(answer.text, "info");
        // The only way to stop the model from here. OpenCode then reports a server error.
        throw new Error(HANDLED);
      }
      if (parents.has(id)) return; // A subagent's brief, which its parent's tool call has shown.
      const { agent, model } = output.message;
      targets.set(id, {
        agent,
        model: { providerID: model.providerID, modelID: model.modelID },
        ...(input.variant ? { variant: input.variant } : {}),
      });
      await start(id, model.modelID);
      // What we steered in ourselves has been announced already, by the mail it came from.
      if (steered.delete(text) && busy.has(id)) return;
      await forward(id, "UserPromptSubmit", { prompt: text, ...(busy.has(id) ? { mid_turn: true } : {}) });
    },

    "tool.execute.before": async (input, output) => {
      const command = typeof output.args?.command === "string" ? output.args.command : "";
      const asked = AGENT_SWITCH.exec(command);
      if (asked !== null) {
        // The command never runs: the agent reads the result as the refusal's reason.
        const argv = ["switch", asked[1] ?? "status", "--by-agent", "--session", input.sessionID];
        const words = await run("voice-copilot", argv);
        throw new Error(`The plugin carried this out in place of the command, so it did work and there is nothing to retry. ${words ?? `Not installed. ${INSTALL}`}`);
      }
      const reply = await forward(input.sessionID, "PreToolUse", {
        tool_name: input.tool,
        tool_use_id: input.callID,
        tool_input: output.args,
      });
      const decided = reply?.hookSpecificOutput;
      if (decided?.permissionDecision === "deny") {
        throw new Error(decided.permissionDecisionReason ?? "Stopped by Voice Copilot.");
      }
    },

    // The model's next step is built once this returns. Reading here as well as on the timer
    // puts a message said during the tool into that step, rather than the one after it.
    "tool.execute.after": () => readMail(),
  };
}
