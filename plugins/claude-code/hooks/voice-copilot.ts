import type { EngineInterface, Register } from 'claude-code'

// Reports this session to the local Voice Copilot companion, and says the user's
// messages to the agent as the user. Runs inside Claude Code, so a tool call costs a
// local HTTP request rather than a process start. Nothing leaves this machine here:
// the companion listens on 127.0.0.1 and its token is in a file only this user reads.

const TOKEN_HEADER = 'X-Voice-Copilot-Token'
const CLI = 'claude'
// Each request is two lines in the debug log, which the IDE extension always keeps. It can't
// wait at the companion for mail: Claude Code doesn't exit while a request is open.
const MAIL_EVERY_MS = 2000
const INSTALL = 'Install or update it with `uv tool install -U voice-copilot`, then start a new session.'

// Might be one of the `vc` words. `voice-copilot-hook --chat` decides, strictly: the whole
// message has to fit, so talking about it is safe.
const MAYBE_A_COMMAND = /^\s*vc(\s|$)/i
const NOT_A_COMMAND_EXIT = 3
// Run by the agent when asked in words (the plugin's skill tells it to).
const AGENT_SWITCH = /^\s*voice-copilot\s+switch\s+(on|off|status)\s*;?\s*$/

type Companion = { host: string; port: number; token: string }
type Answer = { text: string; choices?: string[]; pick?: string }
type Reply = Record<string, unknown> & {
  decision?: string
  reason?: string
  systemMessage?: string
  hookSpecificOutput?: {
    permissionDecision?: 'allow' | 'ask' | 'deny'
    permissionDecisionReason?: string
    additionalContext?: string
  }
}

async function runtimeDir($: EngineInterface): Promise<string> {
  const override = await $.env.get('VOICE_COPILOT_RUNTIME_DIR')
  if (override) return override
  const appData = await $.env.get('LOCALAPPDATA')
  if (appData) return `${appData}/voice-copilot`
  const home = (await $.env.get('HOME')) ?? ''
  if (await $.fs.exists(`${home}/Library/Application Support`)) {
    return `${home}/Library/Application Support/voice-copilot`
  }
  return `${(await $.env.get('XDG_CONFIG_HOME')) ?? `${home}/.config`}/voice-copilot`
}

// `vc off` leaves a word for this session; `vc off all` turns off whatever has no word.
async function isSwitchedOff($: EngineInterface): Promise<boolean> {
  const dir = await runtimeDir($)
  const sid = (await $.session.id()).replace(/[^\w.-]/g, '_')
  try {
    return (await $.fs.read(`${dir}/sessions/${sid}`)) === 'off'
  } catch {
    return $.fs.exists(`${dir}/switched-off`)
  }
}

// What `voice-copilot-hook` makes of this message, or null if it is for the agent.
async function runChat($: EngineInterface, text: string): Promise<Answer | null> {
  const sid = await $.session.id()
  const argv = ['voice-copilot-hook', '--chat', CLI, '--session', sid, '--', text]
  try {
    const ran = await $.process.run(argv, { timeoutMs: 60_000 })
    if (ran.exitCode === NOT_A_COMMAND_EXIT) return null
    if (ran.exitCode === 0) return JSON.parse(ran.stdout) as Answer
  } catch {
    // Not installed: said below.
  }
  return { text: `Voice Copilot's companion is missing or out of date. ${INSTALL}` }
}

// One row each: a row is drawn on one line, which a list doesn't survive.
function showLines($: EngineInterface, text: string): void {
  for (const line of text.split('\n')) $.ui.log(line.trim())
}

// Where the choice fits on Claude Code's own question: it takes two to four.
async function offerChoices($: EngineInterface, answer: Answer): Promise<void> {
  const { choices, pick } = answer
  if (!choices || !pick || choices.length < 2 || choices.length > 4) return
  pickerOpen = true
  try {
    const chosen = await $.ui.ask('Voice Copilot: which one?', choices)
    const done = choices.includes(chosen) ? await runChat($, `${pick} ${chosen}`) : null
    if (done) showLines($, done.text)
  } catch {
    // Dismissed, which is an answer too.
  } finally {
    pickerOpen = false
  }
}

async function companion($: EngineInterface): Promise<Companion | null> {
  try {
    const doc = JSON.parse(await $.fs.read(`${await runtimeDir($)}/companion.json`))
    return { host: doc.host, port: doc.port, token: doc.token }
  } catch {
    return null
  }
}

// voice-copilot's own command owns the switch and starting the companion, so there
// is one implementation of each. Answers null when the package isn't installed, or
// is too old to know the command.
async function runVoiceCopilot(
  $: EngineInterface,
  argv: string[],
  stdin?: string,
): Promise<string | null> {
  try {
    const ran = await $.process.run(argv, { stdin, timeoutMs: 60_000 })
    return ran.exitCode === 0 ? ran.stdout.trim() : null
  } catch {
    return null
  }
}

async function post(
  $: EngineInterface,
  to: Companion,
  body: unknown,
): Promise<Reply | null> {
  const response = await $.http.fetch(`http://${to.host}:${to.port}/api/hooks/${CLI}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', [TOKEN_HEADER]: to.token },
    body: JSON.stringify(body),
  })
  return response.ok && response.text ? (JSON.parse(response.text) as Reply) : null
}

// Whatever goes wrong here, the agent carries on: this reports on it, it isn't part of it.
async function forward($: EngineInterface, body: unknown): Promise<Reply | null> {
  if (await isSwitchedOff($)) return null
  const to = await companion($)
  if (to === null) return null
  try {
    return await post($, to, body)
  } catch {
    return null
  }
}

// VC's own narrator calling `claude -p`, or a CLI already narrated through the proxy.
async function narrated($: EngineInterface): Promise<boolean> {
  return (await $.env.get('VOICE_COPILOT_NO_HOOKS')) === undefined
}

// A dropped prompt goes back in the box, and the next thing typed would join it. It may be
// a while before a queued one gets here, so anything else in the box is a new draft.
async function afterCommand($: EngineInterface, answer: Answer): Promise<void> {
  if (MAYBE_A_COMMAND.test((await $.prompt.read()).text)) {
    await $.prompt.fill({ text: '', mode: 'replace' })
  }
  await offerChoices($, answer)
}

async function deliver($: EngineInterface, text: string, turnRunning: boolean): Promise<void> {
  if (turnRunning) {
    // A user-role row in the running turn: the model reads it at its next step, and no
    // tool call has to be refused for that. The screen doesn't draw it, so say so.
    await $.session.append({
      message: { type: 'user', content: [{ type: 'text', text }] },
    })
    $.ui.toast(`Voice Copilot told the agent: ${text}`)
    return
  }
  // A turn of its own: wakes an idle session, in the user's own words.
  await $.prompt.submit({ text, asUser: true })
}

let mainTurn: string | null = null
let checkingMail = false
// Our own question to the user isn't the agent waiting for them.
let pickerOpen = false

async function readMail($: EngineInterface): Promise<void> {
  if (checkingMail) return
  checkingMail = true
  try {
    if (await isSwitchedOff($)) return
    const to = await companion($)
    if (to === null) return
    // Asked each time: /clear starts a new session with no new `session.start`.
    const sid = await $.session.id()
    const url = `http://${to.host}:${to.port}/api/hooks/${CLI}/mail?session_id=${encodeURIComponent(sid)}`
    const response = await $.http.fetch(url, { headers: { [TOKEN_HEADER]: to.token } })
    if (!response.ok) return
    const { messages, notices } = JSON.parse(response.text) as {
      messages: string[]
      notices?: string[]
    }
    for (const text of notices ?? []) showLines($, text)
    for (const text of messages) {
      try {
        await deliver($, text, mainTurn !== null)
      } catch (failure) {
        await post($, to, {
          hook_event_name: 'PluginError',
          session_id: sid,
          message: `Couldn't say this to the agent: ${text} (${String(failure)})`,
        })
      }
    }
  } catch {
    // The companion went away. Sessions outlive it.
  } finally {
    checkingMail = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!(await narrated($))) return started
    await $.command.register({
      name: 'vc',
      description: 'Voice Copilot: on, off, status, check, speed, volume, model and more',
      argumentHint: '[command]',
      // A `vc check` is for when the agent seems stuck, so it can't wait for the agent.
      immediate: true,
    })
    $.clock.every(MAIL_EVERY_MS, () => readMail($))
    return started
  })

  on('turn.start', ($, e, next) => {
    if (e.agentId === undefined) mainTurn = e.turnId
    return next(e)
  })
  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined) mainTurn = null
    return next(e)
  })

  on('classic.SessionStart', async ($, e, next) => {
    if (!(await narrated($)) || (await isSwitchedOff($))) return next(e)
    // Only a new session may start the companion: its own command does it.
    const ran = await runVoiceCopilot($, ['voice-copilot-hook', CLI], JSON.stringify(e))
    if (ran === null) $.ui.log(`Voice Copilot's companion is missing or out of date. ${INSTALL}`)
    else if (ran) $.ui.log((JSON.parse(ran) as Reply).systemMessage ?? '')
    return next(e)
  })

  on('command.run', { command: 'vc' }, async ($, e) => {
    const answer = await runChat($, `vc ${e.args}`.trim())
    // Shown rather than returned: what a command returns, the model reads too.
    showLines($, answer?.text ?? `Not a Voice Copilot command: vc ${e.args}. For the list: /vc`)
    if (answer) $.clock.after(0, () => void offerChoices($, answer))
    return {}
  })

  on('prompt.submit', async ($, e, next) => {
    if (e.command !== undefined || !MAYBE_A_COMMAND.test(e.text) || !(await narrated($))) {
      return next(e)
    }
    const answer = await runChat($, e.text)
    if (answer === null) return next(e)
    $.clock.after(0, () => void afterCommand($, answer))
    if (!answer.text.includes('\n')) return { drop: answer.text }
    showLines($, answer.text)
    return { drop: 'Voice Copilot' }
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    if (!(await narrated($))) return next(e)
    const reply = await forward($, e)
    const context = reply?.hookSpecificOutput?.additionalContext
    if (context === undefined) return next(e)
    const passed = await next(e)
    return { ...passed, additionalContext: [...(passed.additionalContext ?? []), context] }
  })

  // Not `classic.PreToolUse`: its input doesn't say whether a sub-agent is calling.
  on('tool.call', async ($, e, next) => {
    if (!(await narrated($))) return next(e)
    const { tool, tool_use_id, agentId, requestMeta, ...tool_input } = e as unknown as Record<
      string,
      unknown
    >
    const command = typeof tool_input.command === 'string' ? tool_input.command : ''
    const asked = AGENT_SWITCH.exec(command)
    if (asked !== null) {
      // The command never runs: the agent reads the result as the refusal's reason.
      const action = asked[1] ?? 'status'
      const sid = await $.session.id()
      const argv = ['voice-copilot', 'switch', action, '--by-agent', '--session', sid]
      const words = (await runVoiceCopilot($, argv)) ?? `The companion isn't installed. ${INSTALL}`
      if (action === 'off') $.ui.log(`The agent ran the switch. ${words}`)
      return { deny: `The plugin carried this out in place of the command, so it did work and there is nothing to retry. ${words}` }
    }
    const reply = await forward($, {
      hook_event_name: 'PreToolUse',
      session_id: await $.session.id(),
      cwd: await $.session.cwd(),
      tool_name: tool,
      tool_use_id,
      tool_input,
      ...(agentId === undefined ? {} : { agent_id: agentId }),
    })
    // Said just now, it goes with the step after this tool rather than the one after that.
    if (agentId === undefined) await readMail($)
    const denied = reply?.hookSpecificOutput
    if (denied?.permissionDecision === 'deny') {
      return { deny: denied.permissionDecisionReason ?? 'Stopped by Voice Copilot.' }
    }
    return next(e)
  })

  on('classic.PostToolUse', async ($, e, next) => {
    if (await narrated($)) await forward($, e)
    return next(e)
  })
  on('classic.PostToolUseFailure', async ($, e, next) => {
    if (await narrated($)) await forward($, e)
    return next(e)
  })
  on('classic.SubagentStop', async ($, e, next) => {
    if (await narrated($)) await forward($, e)
    return next(e)
  })
  on('classic.Notification', async ($, e, next) => {
    if (!pickerOpen && (await narrated($))) await forward($, e)
    return next(e)
  })
  on('classic.SessionEnd', async ($, e, next) => {
    if (await narrated($)) await forward($, e)
    return next(e)
  })
  on('classic.Stop', async ($, e, next) => {
    if (!(await narrated($))) return next(e)
    // The plugin reads its own mail, but if that loop died this is the fallback.
    const reply = await forward($, e)
    if (reply?.decision === 'block') return { block: reply.reason ?? '' }
    return next(e)
  })
}
