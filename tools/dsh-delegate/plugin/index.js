/**
 * Host half of the delegate bridge bundle.
 *
 * Publishes `/delegate/v1` on the running Harness web server so an external
 * agent (through its SKILL script) can list and create Workspaces, create
 * Sessions inside a Workspace, prompt a Session with a permission preset, and
 * wait for that turn's final assistant text.
 *
 * The surface is loopback-only and token-authenticated: named web-server
 * routes bypass the Connection trust fence, so this module enforces its own
 * request policy (no browser Origin, bearer token, JSON body, size limit).
 *
 * Turn completion is event-driven: a prompt is accepted, then the bridge
 * listens for `session/event` (`assistant/message`, `turn/end`) and
 * `agent/status`. Job views are persisted under `$DSH_HOME/delegate-jobs` so a
 * reload or restart can still return terminal answers and re-attach non-terminal
 * jobs to their live Sessions.
 *
 * Long-running work is expected: the observation window defaults to 12 hours,
 * extends itself while the Session still runs / waits for a human / is parked
 * on background work, and reports `lost` only for an idle Session with no turn
 * end to observe.
 */

import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { appendFile, chmod, mkdir, readFile, rename, stat, unlink, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'

import {
  HARD_EXTENSION_MS,
  classifySessionState,
  decideExpiry,
  extensionLogEntry,
  graceMsFor,
  normalizeExpireMs,
  settleFromTurnEnd,
  settlementLogEntry,
  textOfAssistantMessage,
  transitionLogEntry,
} from './lib/job-policy.js'
import { JobStore, fromRecord, toRecord } from './lib/job-store.js'
import {
  answerLogEntry,
  normalizeApprovalDecision,
  pendingSummaryLine,
  summarizePendingFrame,
  validateQuestionAnswer,
} from './lib/pending-requests.js'

export const name = 'dsh-delegate'

export const inject = [
  'webServer',
  'sessions',
  'agents',
  'sessionController',
  'workspaceRegistry',
  'permissionPresets',
  'connection',
  'typertGateway',
]

const ROUTE_PREFIX = '/delegate/v1'
const PLUGIN_VERSION = '0.4.1'
const MAX_BODY_BYTES = 256 * 1024
const DEFAULT_WAIT_MS = 60_000
const MAX_WAIT_MS = 240_000
const PERSIST_THROTTLE_MS = 1000
const RECOVERY_PAGE_MESSAGES = 300
const MEMORY_RETAIN_MS = 10 * 60 * 1000

/**
 * Bounded observation window for one per-job watcher round. The watcher rides
 * `sessionController.wait`, which itself samples passive state (including
 * pending approval/question inputs) every second; this constant is only the
 * outer round length.
 */
const WATCH_WINDOW_MS = (() => {
  const raw = Number(process.env.DSH_DELEGATE_WATCH_MS)
  return Number.isFinite(raw) && raw >= 500 ? Math.trunc(raw) : 5_000
})()

/** Reconnect delay for the forwarded-event answering client. */
const EVENT_RECONNECT_MS = 1_000

/** Recent session events scanned when enriching an approval with its tool call. */
const TOOL_CALL_LOOKUP_MESSAGES = 400

/** Bound on retained "last answer" records (one per session). */
const LAST_DECISIONS_LIMIT = 200

/** Hard extension after the observation window; env override exists for e2e runs. */
const HARD_EXTENSION_LIMIT = (() => {
  const raw = Number(process.env.DSH_DELEGATE_HARD_EXTENSION_MS)
  return Number.isFinite(raw) && raw > 0 ? Math.trunc(raw) : HARD_EXTENSION_MS
})()

/** @param {unknown} value */
function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/** @param {number} ms */
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** @param {unknown} error */
function messageOf(error) {
  if (error instanceof Error) return error.message
  try {
    return String(error)
  } catch {
    return 'unknown error'
  }
}

class HttpError extends Error {
  /**
   * @param {number} status
   * @param {string} code
   * @param {string} message
   */
  constructor(status, code, message) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** @param {import('node:http').ServerResponse} res */
function sendJson(res, status, body) {
  const payload = Buffer.from(JSON.stringify(body))
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.setHeader('content-length', String(payload.length))
  res.end(payload)
}

/** @param {import('node:http').IncomingMessage} req */
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new HttpError(413, 'body_too_large', `Request body exceeds ${MAX_BODY_BYTES} bytes.`))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (size === 0) {
        resolve({})
        return
      }
      let value
      try {
        value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch {
        reject(new HttpError(400, 'invalid_json', 'Request body must be valid JSON.'))
        return
      }
      if (!isRecord(value)) {
        reject(new HttpError(400, 'invalid_json', 'Request body must be a JSON object.'))
        return
      }
      resolve(value)
    })
    req.on('error', reject)
  })
}

/** A one-shot caller signal for reads that must not cancel agent execution. */
function requestSignal() {
  return new AbortController().signal
}

/**
 * Host plugin entry.
 * @param {import('cordis').Context} ctx
 */
export function apply(ctx) {
  const token = randomBytes(32).toString('hex')
  const dshHome = process.env.DSH_HOME && process.env.DSH_HOME.trim() !== '' ? process.env.DSH_HOME : join(homedir(), '.dsh')
  const discoveryPath = join(dshHome, 'delegate.json')
  const store = new JobStore(join(dshHome, 'delegate-jobs'))
  const startedAt = new Date().toISOString()

  /** @type {Map<string, any>} */
  const jobs = new Map()
  /** @type {Map<string, string>} sessionId -> active job id */
  const activeJobs = new Map()
  /** @type {Map<string, any>} forwarded requestId -> pending request summary + clientId */
  const pendingRequests = new Map()
  /** @type {Map<string, any>} sessionId -> last bridge answer record */
  const lastDecisions = new Map()
  /** Forwarded-event answering client state (one owner Peer, one `$events` stream). */
  const eventClient = { peer: undefined, controller: undefined, clientId: undefined, status: 'stopped' }

  // ---------------------------------------------------------------- discovery

  async function publishDiscovery() {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        const origin = ctx.webServer.getOrigin()
        const document = {
          version: 1,
          plugin: 'dsh-delegate',
          pluginVersion: PLUGIN_VERSION,
          origin,
          routePrefix: ROUTE_PREFIX,
          token,
          pid: process.pid,
          startedAt,
        }
        await mkdir(dshHome, { recursive: true })
        await writeFile(discoveryPath, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o600 })
        await chmod(discoveryPath, 0o600)
        return
      } catch {
        await delay(100)
      }
    }
    ctx.logger?.warn?.(`dsh-delegate: could not publish ${discoveryPath}`)
  }

  async function removeDiscovery() {
    try {
      const raw = await readFile(discoveryPath, 'utf8')
      const document = JSON.parse(raw)
      if (document?.pid === process.pid && document?.token === token) await unlink(discoveryPath)
    } catch {
      // Already gone, another instance owns it, or it was never written.
    }
  }

  // ------------------------------------------------------------------- jobs

  function jobView(job) {
    return {
      jobId: job.id,
      sessionId: job.sessionId,
      state: job.state,
      text: job.text,
      reason: job.turnEnd?.reason?.kind ?? null,
      turn: job.turnEnd?.turn ?? null,
      error: job.error ?? null,
      pendingInput: Array.isArray(job.pendingInput) ? job.pendingInput : [],
      extensions: job.extensions ?? 0,
      lastObserved: job.lastObserved ?? null,
      pending: pendingForSession(job.sessionId),
      lastDecision: lastDecisions.get(job.sessionId) ?? null,
      expireAt: job.expireAt,
      createdAt: job.createdAt,
      updatedAt: job.updatedAt,
    }
  }

  function persistJob(job, force = false) {
    const now = Date.now()
    if (force) {
      job.lastPersist = now
      void store.save(toRecord(job)).catch(() => {})
      return
    }
    if (now - (job.lastPersist ?? 0) < PERSIST_THROTTLE_MS) {
      if (job.persistPending === true) return
      job.persistPending = true
      setTimeout(() => {
        job.persistPending = false
        if (job.settled || jobs.has(job.id)) persistJob(job, true)
      }, PERSIST_THROTTLE_MS).unref?.()
      return
    }
    job.lastPersist = now
    void store.save(toRecord(job)).catch(() => {})
  }

  function notifyJob(job) {
    job.updatedAt = Date.now()
    const waiters = [...job.waiters]
    job.waiters.clear()
    for (const waiter of waiters) {
      try {
        waiter()
      } catch {
        // A waiter must never break the job.
      }
    }
  }

  function emitJobState(job, state) {
    if (job.settled || job.state === state) return
    const previous = job.state
    job.state = state
    if (state === 'input-required') {
      logJobEvent(transitionLogEntry(job, 'delegate.job.input-required'))
    } else if (previous === 'input-required' && state === 'running') {
      logJobEvent(transitionLogEntry(job, 'delegate.job.resumed'))
    }
    notifyJob(job)
    persistJob(job)
  }

  function disposeJobListeners(job) {
    for (const dispose of job.disposers.splice(0)) {
      try {
        dispose()
      } catch {
        // best effort
      }
    }
    if (job.expiryTimer !== undefined) {
      clearTimeout(job.expiryTimer)
      job.expiryTimer = undefined
    }
    if (job.cancelTimer !== undefined) {
      clearTimeout(job.cancelTimer)
      job.cancelTimer = undefined
    }
    if (job.watchAbort !== undefined) {
      job.watchAbort.abort()
      job.watchAbort = undefined
    }
  }

  /** Apply one confirmed passive-state sample to a job (settle / input / running / suspended). */
  function applyStateSample(job, state) {
    if (job.settled || state === undefined) return
    observeSessionState(job, state)
    if (job.turnEnd !== null) {
      settleFromReason(job, job.turnEnd.reason)
      return
    }
    const observed = classifySessionState(state)
    if (observed === 'input-required') {
      emitJobState(job, 'input-required')
      return
    }
    if (observed === 'running') {
      job.suspended = false
      emitJobState(job, 'running')
      return
    }
    job.suspended = true
    emitJobState(job, 'suspended')
  }

  /**
   * Observe one job until it settles. Uses DSH's own bounded `session/wait`
   * rather than a bespoke poll: it samples passive state (pending approvals and
   * questions included) every second and returns immediately on
   * `input-required`, which is how the browser UI stays current too.
   */
  function startJobWatch(job) {
    if (job.watchAbort !== undefined) return
    const controller = new AbortController()
    job.watchAbort = controller
    void watchJob(job, controller.signal)
  }

  async function watchJob(job, signal) {
    while (!job.settled && !signal.aborted) {
      if (!job.started) {
        // The prompt may still be queued behind another turn; an idle sample
        // before this job's turn starts must not read as "suspended".
        await delay(500)
        continue
      }
      let result
      try {
        result = await ctx.sessionController.wait(
          { sessionId: job.sessionId, timeoutMs: WATCH_WINDOW_MS },
          signal,
        )
      } catch (error) {
        if (job.settled || signal.aborted) return
        const code = typeof error?.code === 'string' ? error.code : 'unknown'
        if (code === 'session/not-found') {
          settleJob(job, 'lost', {
            code: 'session-gone',
            message: `The Session is no longer available (${code}). Use "last --session" to read any persisted answer.`,
          })
          return
        }
        job.lastReadError = { code, message: messageOf(error) }
        job.unreadableSince ??= Date.now()
        persistJob(job)
        await delay(WATCH_WINDOW_MS)
        continue
      }
      if (job.settled || signal.aborted) return
      if (result?.state !== undefined) applyStateSample(job, result.state)
      // `running` spent the whole window already; pending input / idle return
      // immediately, so a short pause keeps this loop from spinning.
      if (result?.reason !== 'timeout') await delay(1_000)
    }
  }

  const jobLogPath = join(dshHome, 'delegate-jobs', 'events.jsonl')
  const JOB_LOG_MAX_BYTES = 8 * 1024 * 1024

  /**
   * Append one structured job event as JSONL under `$DSH_HOME/delegate-jobs`.
   * The log is the durable answer to "why did this job keep extending?" and
   * must never break job settlement, so every failure is swallowed.
   */
  async function appendJobLog(entry) {
    const line = `${JSON.stringify({ time: new Date().toISOString(), ...entry })}\n`
    try {
      await mkdir(join(dshHome, 'delegate-jobs'), { recursive: true })
      try {
        const info = await stat(jobLogPath)
        if (info.size > JOB_LOG_MAX_BYTES) await rename(jobLogPath, `${jobLogPath}.1`)
      } catch {
        // No log yet (or stat failed); append creates it.
      }
      await appendFile(jobLogPath, line, { mode: 0o600 })
    } catch {
      // Diagnostics are best effort.
    }
    try {
      ctx.logger.info(JSON.stringify(entry))
    } catch {
      // The profile logger is optional in some compositions.
    }
  }

  function logJobEvent(entry) {
    void appendJobLog(entry)
  }

  function settleJob(job, state, error) {
    if (job.settled) return
    job.settled = true
    job.state = state
    if (error !== undefined) job.error = error
    logJobEvent(settlementLogEntry(job, state, error ?? null))
    disposeJobListeners(job)
    if (activeJobs.get(job.sessionId) === job.id) activeJobs.delete(job.sessionId)
    notifyJob(job)
    persistJob(job, true)
    setTimeout(() => {
      if (jobs.get(job.id) === job) jobs.delete(job.id)
    }, MEMORY_RETAIN_MS).unref?.()
  }

  function settleFromReason(job, reason) {
    const outcome = settleFromTurnEnd(reason)
    settleJob(job, outcome.state, outcome.error)
  }

  function scheduleExpiry(job) {
    if (job.settled) return
    if (job.expiryTimer !== undefined) clearTimeout(job.expiryTimer)
    const wait = Math.max(0, (job.expireAt ?? Date.now()) - Date.now())
    job.expiryTimer = setTimeout(() => {
      void onExpiry(job)
    }, wait)
    job.expiryTimer.unref?.()
  }

  async function readSessionStateResult(sessionId) {
    try {
      return { state: await ctx.sessionController.state({ sessionId }, requestSignal()) }
    } catch (error) {
      return {
        error: {
          code: typeof error?.code === 'string' ? error.code : 'unknown',
          message: messageOf(error),
        },
      }
    }
  }

  /** Record what the latest readable sample said, for expiry diagnostics. */
  function observeSessionState(job, state) {
    const pending = Array.isArray(state.pendingInput) ? state.pendingInput : []
    if (pending.length > 0) job.pendingInput = pending
    job.lastObserved =
      state.running === true
        ? 'running'
        : pending.length > 0
          ? `input-required(${pending.join(',')})`
          : job.suspended === true
            ? 'suspended'
            : 'idle'
    if (state.running === true) job.suspended = false
  }

  async function onExpiry(job) {
    if (job.settled) return
    if (job.turnEnd !== null) {
      settleFromReason(job, job.turnEnd.reason)
      return
    }
    const now = Date.now()
    const result = await readSessionStateResult(job.sessionId)
    if (job.settled) return
    if (result.state !== undefined) {
      job.unreadableStreak = 0
      job.unreadableSince = null
      job.lastReadError = null
      observeSessionState(job, result.state)
    } else {
      job.lastReadError = result.error
      job.unreadableStreak = (job.unreadableStreak ?? 0) + 1
      job.unreadableSince ??= now
    }
    const decision = decideExpiry({
      hasTurnEnd: job.turnEnd !== null,
      sessionState: result.state,
      readError: result.error,
      suspended: job.suspended === true,
      createdAt: job.createdAt,
      now,
      expireMs: job.expireDuration,
      hardExtensionMs: HARD_EXTENSION_LIMIT,
      extensions: job.extensions ?? 0,
      unreadableStreak: job.unreadableStreak ?? 0,
      unreadableSince: job.unreadableSince ?? null,
      lastObserved: job.lastObserved ?? null,
    })
    if (decision.action === 'extend') {
      logJobEvent(extensionLogEntry(job, decision.reason))
      job.extensions = (job.extensions ?? 0) + 1
      if (decision.reason !== 'state-unreadable') {
        job.unreadableStreak = 0
        job.unreadableSince = null
      }
      job.expireAt = now + graceMsFor(job.expireDuration)
      scheduleExpiry(job)
      persistJob(job)
      return
    }
    settleJob(job, decision.state, decision.error)
  }

  /**
   * One-shot refresh used by event handlers and every `GET /jobs` request.
   * The per-job watcher covers the no-event cases (approval waits).
   */
  async function checkJob(job) {
    if (job.settled) return
    if (job.checking) {
      job.recheck = true
      return
    }
    job.checking = true
    try {
      const agent = ctx.agents.get(job.sessionId)
      if (agent === undefined) return
      if (agent.status === 'running') job.started = true
      if (!job.started) return
      const result = await readSessionStateResult(job.sessionId)
      if (job.settled) return
      if (result.state !== undefined) {
        applyStateSample(job, result.state)
        return
      }
      if (result.error?.code === 'session/not-found') {
        settleJob(job, 'lost', {
          code: 'session-gone',
          message: `The Session is no longer available (${result.error.code}). Use "last --session" to read any persisted answer.`,
        })
      }
    } finally {
      job.checking = false
      if (job.recheck) {
        job.recheck = false
        void checkJob(job)
      }
    }
  }

  function attachJobListeners(job) {
    job.disposers.push(
      ctx.on('session/event', (session, event) => {
        if (job.settled) return
        if (session?.id !== job.sessionId) return
        if (typeof event?.seq !== 'number' || event.seq <= job.afterSeq) return
        if (event.type === 'assistant/message') {
          job.text = textOfAssistantMessage(event.data?.message)
          job.updatedAt = Date.now()
          persistJob(job)
          return
        }
        if (event.type === 'turn/end') {
          job.turnEnd = { reason: event.data?.reason, turn: event.data?.turn, seq: event.seq }
          void checkJob(job)
        }
      }),
    )
    job.disposers.push(
      ctx.on('agent/status', (payload) => {
        if (job.settled) return
        if (payload?.agent?.id !== job.sessionId) return
        if (payload.status === 'running') {
          job.started = true
          job.suspended = false
        }
        void checkJob(job)
      }),
    )
    job.disposers.push(
      ctx.on('agent/error', (payload) => {
        if (job.settled) return
        if (payload?.agent?.id !== job.sessionId) return
        job.lastError = payload.error
      }),
    )
    job.disposers.push(
      ctx.on('agent/disposed', (payload) => {
        if (job.settled) return
        if (payload?.agent?.id !== job.sessionId) return
        settleJob(job, 'failed', {
          code: 'agent_disposed',
          message: 'The Session Agent was disposed before the turn finished.',
        })
      }),
    )
  }

  function newJob({ sessionId, afterSeq, expireMs }) {
    const now = Date.now()
    const job = {
      id: randomUUID(),
      sessionId,
      requestId: randomUUID(),
      afterSeq,
      started: false,
      settled: false,
      checking: false,
      recheck: false,
      state: 'running',
      text: '',
      turnEnd: null,
      error: null,
      pendingInput: [],
      suspended: false,
      extensions: 0,
      lastObserved: null,
      lastReadError: null,
      unreadableStreak: 0,
      unreadableSince: null,
      createdAt: now,
      updatedAt: now,
      expireAt: now + expireMs,
      expireDuration: expireMs,
      lastPersist: 0,
      persistPending: false,
      waiters: new Set(),
      disposers: [],
      expiryTimer: undefined,
      cancelTimer: undefined,
    }
    jobs.set(job.id, job)
    activeJobs.set(sessionId, job.id)
    attachJobListeners(job)
    scheduleExpiry(job)
    startJobWatch(job)
    persistJob(job, true)
    return job
  }

  async function readRecentTurns(sessionId, { turnsLimit = 1, sinceSeq } = {}) {
    let page
    try {
      page = await ctx.sessionController.events(
        {
          address: { kind: 'session', sessionId },
          maxMessages: RECOVERY_PAGE_MESSAGES,
        },
        requestSignal(),
      )
    } catch (error) {
      fail(error)
    }
    /** @type {Map<number, { turn: number, text: string, seq: number }>} */
    const byTurn = new Map()
    let lastTurnEnd = null
    for (const record of page.records ?? []) {
      const event = record?.event
      if (event === undefined) continue
      if (typeof sinceSeq === 'number' && typeof event.seq === 'number' && event.seq <= sinceSeq) continue
      if (event.type === 'assistant/message') {
        const turn = event.data?.turn
        const text = textOfAssistantMessage(event.data?.message)
        if (typeof turn === 'number' && text !== '') {
          byTurn.set(turn, { turn, text, seq: event.seq })
        }
        continue
      }
      if (event.type === 'turn/end') {
        lastTurnEnd = { reason: event.data?.reason, turn: event.data?.turn, seq: event.seq }
      }
    }
    const turns = [...byTurn.values()].sort((a, b) => a.turn - b.turn)
    return { turns: turns.slice(-Math.max(1, turnsLimit)), turnEnd: lastTurnEnd }
  }

  async function recoverRecord(record) {
    if (record === undefined || record === null) return undefined
    if (record.settled === true) return fromRecord(record)
    const job = fromRecord(record)
    jobs.set(job.id, job)
    activeJobs.set(job.sessionId, job.id)
    job.waiters = new Set()
    job.disposers = []
    attachJobListeners(job)
    job.expireDuration = Math.max(1000, (record.expireAt ?? Date.now()) - (record.createdAt ?? Date.now()))
    scheduleExpiry(job)
    startJobWatch(job)
    try {
      const history = await readRecentTurns(job.sessionId, { turnsLimit: 1, sinceSeq: job.afterSeq })
      if (history.turnEnd !== null) {
        const text = history.turns.find((turn) => turn.turn === history.turnEnd.turn)?.text
        if (text !== undefined) job.text = text
        job.turnEnd = history.turnEnd
        settleFromReason(job, history.turnEnd.reason)
        return job
      }
    } catch {
      // Fall through to liveness checks.
    }
    if (ctx.agents.get(job.sessionId) === undefined) {
      settleJob(job, 'lost', {
        code: 'lost',
        message:
          'The bridge was restarted while this job was running and the Session has no live Agent; the turn result is no longer observable. Use "last --session" to read the persisted answer.',
      })
      return job
    }
    job.started = true
    void checkJob(job)
    return job
  }

  async function getJob(jobId) {
    const inMemory = jobs.get(jobId)
    if (inMemory !== undefined) return inMemory
    const record = await store.load(jobId)
    if (record === undefined) return undefined
    return recoverRecord(record)
  }

  async function awaitJob(job, timeoutMs) {
    if (job.settled) return
    if (job.state !== 'running' && job.state !== 'suspended' && job.state !== 'cancelling') return
    return new Promise((resolve) => {
      let done = false
      const finish = () => {
        if (done) return
        done = true
        clearTimeout(timer)
        job.waiters.delete(finish)
        resolve()
      }
      const timer = setTimeout(finish, Math.max(1, Math.min(timeoutMs, MAX_WAIT_MS)))
      job.waiters.add(finish)
      if (job.settled || job.state === 'input-required') finish()
    })
  }

  // --------------------------------------------------------------- utilities

  /** @param {unknown} error */
  function fail(error) {
    if (error instanceof HttpError) throw error
    const remote = error && typeof error === 'object' ? error : {}
    const code = typeof remote.code === 'string' ? remote.code : 'internal'
    if (code === 'session/not-found') {
      throw new HttpError(404, code, messageOf(error))
    }
    if (code === 'session/agent-busy' || code === 'session/writer-held') {
      throw new HttpError(409, code, messageOf(error))
    }
    if (code === 'gateway/bad-request') {
      throw new HttpError(400, code, messageOf(error))
    }
    throw new HttpError(500, code, messageOf(error))
  }

  async function resolveLiveSession(sessionId) {
    let resolved
    try {
      resolved = await ctx.sessionController.resolveAgent(sessionId)
    } catch (error) {
      fail(error)
    }
    if (resolved?.error !== undefined) fail(resolved.error)
    return resolved.agent.session
  }

  async function applyPermission(session, preset) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        ctx.permissionPresets.set(session, preset)
        return
      } catch (error) {
        if (attempt === 0 && messageOf(error).includes('not ready')) {
          await delay(150)
          continue
        }
        throw new HttpError(409, 'permission_unavailable', messageOf(error))
      }
    }
  }

  // ------------------------------------------------------ forwarded events
  //
  // The GUI answers approvals and questions as a forwarded `$events` client:
  // Cordis waterfalls are projected to the browser and answered through
  // `$events/result`. The bridge joins as its own owner answering Peer, so it
  // sees exactly the same requests; whichever side answers first settles the
  // waterfall and the gateway cancels the other delivery, which is what closes
  // the GUI card.

  function emptyUplink() {
    return (async function* emptyEventsUplink() {})()
  }

  function onForwardedWaterfall(frame) {
    const summary = summarizePendingFrame(frame)
    if (summary === undefined) return
    pendingRequests.set(summary.requestId, { ...summary, clientId: eventClient.clientId })
  }

  function stripClientId(entry) {
    const { clientId: _clientId, ...rest } = entry
    return rest
  }

  function pendingForSession(sessionId) {
    const rows = []
    for (const entry of pendingRequests.values()) {
      if (entry.sessionId === sessionId) rows.push(stripClientId(entry))
    }
    rows.sort((a, b) => a.receivedAt - b.receivedAt)
    return rows
  }

  async function runEventClient(signal) {
    while (!signal.aborted) {
      try {
        await consumeEventStream(signal)
      } catch (error) {
        if (signal.aborted) return
        eventClient.status = 'disconnected'
        console.error(`[dsh-delegate] forwarded event stream failed: ${messageOf(error)}`)
      }
      if (signal.aborted) return
      await delay(EVENT_RECONNECT_MS)
    }
  }

  async function consumeEventStream(signal) {
    eventClient.status = 'connecting'
    const source = await ctx.typertGateway.wireStream.open(
      '$events',
      { args: {} },
      emptyUplink(),
      eventClient.peer,
      signal,
    )
    for await (const frame of source) {
      if (signal.aborted) return
      if (frame?.type === 'ready') {
        eventClient.clientId = frame.clientId
        eventClient.status = 'ready'
        continue
      }
      if (frame?.type === 'cancel') {
        pendingRequests.delete(frame.eventId)
        continue
      }
      if (frame?.type === 'waterfall') onForwardedWaterfall(frame)
    }
    throw new Error('forwarded event stream ended')
  }

  function startEventClient() {
    if (eventClient.peer !== undefined) return
    eventClient.peer = ctx.connection.createPeer({ principal: { kind: 'owner' }, interactions: 'answer' })
    eventClient.controller = new AbortController()
    const signal = AbortSignal.any([eventClient.controller.signal, eventClient.peer.signal])
    void runEventClient(signal)
  }

  function stopEventClient() {
    try {
      eventClient.controller?.abort()
    } catch {
      // best effort
    }
    void eventClient.peer?.dispose?.()
    eventClient.peer = undefined
    eventClient.controller = undefined
    eventClient.clientId = undefined
    eventClient.status = 'stopped'
    pendingRequests.clear()
  }

  async function findToolCall(sessionId, callId) {
    try {
      const page = await ctx.sessionController.events(
        { address: { kind: 'session', sessionId }, maxMessages: TOOL_CALL_LOOKUP_MESSAGES },
        requestSignal(),
      )
      for (const record of page.records ?? []) {
        const event = record?.event
        if (event?.type === 'tool/call' && event.data?.callId === callId) {
          return {
            name: typeof event.data.name === 'string' ? event.data.name : null,
            arguments: typeof event.data.arguments === 'string' ? event.data.arguments : '',
            seq: event.seq,
          }
        }
      }
    } catch {
      // Enrichment is best effort; the approval itself stays answerable.
    }
    return undefined
  }

  async function listPending(sessionId) {
    const job = findActiveJob(sessionId)
    const pending = pendingForSession(sessionId)
    for (const entry of pending) {
      if (entry.kind === 'approval' && entry.callId !== null) {
        const toolCall = await findToolCall(sessionId, entry.callId)
        if (toolCall !== undefined) entry.toolCall = toolCall
      }
    }
    return { sessionId, pending, jobId: job?.id ?? null }
  }

  async function answerPending(sessionId, requestId, body, caller) {
    const entry = pendingRequests.get(requestId)
    if (entry === undefined) {
      throw new HttpError(
        409,
        'request-not-pending',
        'The request is not pending: it was answered (possibly in the GUI), cancelled, or never existed.',
      )
    }
    if (entry.sessionId !== sessionId) {
      throw new HttpError(404, 'request-not-found', 'The request does not belong to this session.')
    }

    let decision
    let outcomeValue
    let auditReason
    if (entry.kind === 'approval') {
      decision = normalizeApprovalDecision(body.decision)
      if (decision === undefined) {
        throw new HttpError(400, 'invalid_decision', 'decision must be "allow-once" or "reject".')
      }
      outcomeValue = decision
      auditReason = typeof body.reason === 'string' && body.reason !== '' ? body.reason : undefined
    } else {
      const validated = validateQuestionAnswer(body.answer)
      if (!validated.ok) throw new HttpError(400, 'invalid_answer', validated.message)
      decision = 'answered'
      outcomeValue = { answers: validated.answers }
      auditReason = typeof body.reason === 'string' && body.reason !== '' ? body.reason : undefined
    }

    pendingRequests.delete(requestId)
    let result
    try {
      result = await ctx.typertGateway.dispatchRpc(
        '$events/result',
        { args: { clientId: entry.clientId, eventId: requestId, outcome: { kind: 'result', value: outcomeValue } } },
        requestSignal(),
        eventClient.peer,
      )
    } catch (error) {
      throw new HttpError(409, 'answer-failed', messageOf(error))
    }
    if (result?.ok !== true) {
      throw new HttpError(409, 'answer-failed', result?.error?.message ?? 'The Host rejected the answer.')
    }

    const job = findActiveJob(sessionId)
    const record = answerLogEntry({ job, entry, decision, caller, reason: auditReason })
    logJobEvent(record)
    lastDecisions.set(sessionId, {
      requestId,
      kind: entry.kind,
      decision,
      caller,
      reason: auditReason ?? null,
      at: Date.now(),
    })
    if (lastDecisions.size > LAST_DECISIONS_LIMIT) {
      const oldest = lastDecisions.keys().next().value
      if (oldest !== undefined) lastDecisions.delete(oldest)
    }
    if (job !== undefined) {
      job.lastDecision = lastDecisions.get(sessionId)
      persistJob(job)
      // The settled approval no longer counts as pending input, but the job's
      // cached state still says `input-required`; refresh before returning so an
      // immediate `wait --until-done` sees the resumed turn instead of a stale
      // human-required state.
      await checkJob(job)
    }
    return { requestId, kind: entry.kind, decision, jobId: job?.id ?? null, caller }
  }

  // ------------------------------------------------------------------ routes

  async function listWorkspaces() {
    const archived = new Set(ctx.workspaceRegistry.archivedSessionIds ?? [])
    const rows = ctx.workspaceRegistry.list()
    const workspaces = []
    for (const workspace of rows) {
      let status = 'unknown'
      try {
        status = await workspace.status()
      } catch {
        status = 'unknown'
      }
      const sessionIds = workspace.sessionIds.filter((id) => !archived.has(id))
      workspaces.push({
        id: workspace.id,
        title: workspace.title,
        path: workspace.path,
        status,
        sessionCount: sessionIds.length,
        updatedAt: workspace.updatedAt,
      })
    }
    return { workspaces }
  }

  async function createWorkspace(body) {
    const path = body.path
    if (typeof path !== 'string' || path.trim() === '') {
      throw new HttpError(400, 'invalid_path', 'path is required.')
    }
    if (!isAbsolute(path)) {
      throw new HttpError(400, 'invalid_path', 'path must be absolute.')
    }
    const title = typeof body.title === 'string' && body.title.trim() !== '' ? body.title : undefined
    try {
      await mkdir(path, { recursive: true })
    } catch (error) {
      throw new HttpError(400, 'directory_unavailable', messageOf(error))
    }
    let workspace
    try {
      workspace = await ctx.workspaceRegistry.create(path, title)
    } catch (error) {
      throw new HttpError(400, 'workspace_create_failed', messageOf(error))
    }
    return {
      workspace: {
        id: workspace.id,
        title: workspace.title,
        path: workspace.path,
        sessionCount: workspace.sessionIds.length,
      },
    }
  }

  async function listSessions(url) {
    const workspaceId = url.searchParams.get('workspaceId') ?? undefined
    const includeArchived = url.searchParams.get('includeArchived') === 'true'
    const limitRaw = Number(url.searchParams.get('limit') ?? '100')
    const limit = Number.isSafeInteger(limitRaw) && limitRaw > 0 ? Math.min(limitRaw, 500) : 100
    const archived = new Set(ctx.workspaceRegistry.archivedSessionIds ?? [])
    let workspace
    if (workspaceId !== undefined) {
      workspace = ctx.workspaceRegistry.get(workspaceId)
      if (workspace === undefined) throw new HttpError(404, 'workspace/not-found', `workspace "${workspaceId}" not found`)
    }
    let page
    try {
      page = await ctx.sessionController.list({}, requestSignal())
    } catch (error) {
      fail(error)
    }
    const allowed = workspace === undefined ? undefined : new Set(workspace.sessionIds)
    const sessions = []
    for (const row of page.items) {
      if (allowed !== undefined && !allowed.has(row.sessionId)) continue
      const isArchived = archived.has(row.sessionId)
      if (isArchived && !includeArchived) continue
      sessions.push({
        sessionId: row.sessionId,
        updatedAt: row.updatedAt,
        running: row.running,
        blank: row.blank,
        agentAvailable: row.agentAvailable,
        cwd: row.cwd ?? null,
        origin: row.origin ?? null,
        parentSessionId: row.parentSessionId ?? null,
        archived: isArchived,
      })
    }
    sessions.sort((a, b) => b.updatedAt - a.updatedAt)
    return { sessions: sessions.slice(0, limit), total: sessions.length }
  }

  async function createSession(body) {
    const workspaceId = typeof body.workspaceId === 'string' ? body.workspaceId : undefined
    const cwd = typeof body.cwd === 'string' ? body.cwd : undefined
    if (workspaceId === undefined && cwd === undefined) {
      throw new HttpError(400, 'invalid_location', 'workspaceId or cwd is required.')
    }
    if (workspaceId !== undefined && cwd !== undefined) {
      throw new HttpError(400, 'invalid_location', 'Pass workspaceId or cwd, not both.')
    }
    if (cwd !== undefined && !isAbsolute(cwd)) {
      throw new HttpError(400, 'invalid_path', 'cwd must be absolute.')
    }
    const agentPreset = typeof body.agentPreset === 'string' && body.agentPreset !== '' ? body.agentPreset : undefined
    const sessionId = typeof body.sessionId === 'string' && body.sessionId !== '' ? body.sessionId : undefined
    const title = typeof body.title === 'string' && body.title.trim() !== '' ? body.title : undefined
    let created
    try {
      created = await ctx.sessionController.create({
        ...(workspaceId === undefined ? {} : { workspaceId }),
        ...(cwd === undefined ? {} : { cwd }),
        ...(agentPreset === undefined ? {} : { agentPreset }),
        ...(sessionId === undefined ? {} : { sessionId }),
      })
    } catch (error) {
      const code = error && typeof error === 'object' ? error.code : undefined
      if (code === 'workspace/not-found') throw new HttpError(404, code, messageOf(error))
      if (code === 'agent-preset/not-found') throw new HttpError(400, code, messageOf(error))
      if (code === 'session/conflict' || code === 'session/agent-busy') throw new HttpError(409, code, messageOf(error))
      fail(error)
    }
    if (title !== undefined) {
      try {
        await ctx.sessionController.rename({ sessionId: created.sessionId, title })
      } catch (error) {
        throw new HttpError(409, 'rename_failed', messageOf(error))
      }
    }
    return { sessionId: created.sessionId, agentPreset: created.agentPreset ?? null, workspaceId: workspaceId ?? null }
  }

  async function readSessionState(sessionId) {
    let state
    try {
      state = await ctx.sessionController.state({ sessionId }, requestSignal())
    } catch (error) {
      const code = error && typeof error === 'object' ? error.code : undefined
      if (code === 'session/not-found') throw new HttpError(404, code, messageOf(error))
      fail(error)
    }
    const agent = ctx.agents.get(sessionId)
    let permission = null
    if (agent !== undefined) {
      try {
        permission = ctx.permissionPresets.current(agent.session)
      } catch {
        permission = null
      }
    }
    return {
      sessionId,
      state,
      cwd: agent?.session?.header?.cwd ?? null,
      permission,
    }
  }

  async function lastTurns(sessionId, url) {
    const raw = Number(url.searchParams.get('turns') ?? '1')
    const turnsLimit = Number.isSafeInteger(raw) && raw > 0 ? Math.min(raw, 20) : 1
    const { turns } = await readRecentTurns(sessionId, { turnsLimit })
    return { sessionId, turns }
  }

  function findActiveJob(sessionId) {
    const id = activeJobs.get(sessionId)
    if (id === undefined) return undefined
    const job = jobs.get(id)
    if (job === undefined || job.settled) {
      activeJobs.delete(sessionId)
      return undefined
    }
    return job
  }

  async function promptSession(sessionId, body) {
    const text = body.text
    if (typeof text !== 'string' || text.trim() === '') {
      throw new HttpError(400, 'invalid_text', 'text is required.')
    }
    const mode = body.mode === 'steer' ? 'steer' : 'queue'
    const expireMs = normalizeExpireMs(body.expireMs)
    let permission
    if (typeof body.permission === 'string' && body.permission !== '') {
      const allowed = ctx.permissionPresets.catalog().options.map((option) => option.value)
      if (!allowed.includes(body.permission)) {
        throw new HttpError(400, 'invalid_permission', `permission must be one of: ${allowed.join(', ')}`)
      }
      permission = body.permission
    }
    const existing = findActiveJob(sessionId)
    if (existing !== undefined) {
      throw new HttpError(409, 'session_busy', `Session already has an active delegated job: ${existing.id}`)
    }
    const session = await resolveLiveSession(sessionId)
    if (permission !== undefined) await applyPermission(session, permission)
    const job = newJob({ sessionId, afterSeq: Number(session.seq) - 1, expireMs })
    const waitMs = body.waitMs === undefined ? DEFAULT_WAIT_MS : normalizeWait(body.waitMs)
    try {
      await ctx.sessionController.prompt(
        {
          requestId: job.requestId,
          sessionId,
          mode,
          content: [{ type: 'text', text }],
        },
        requestSignal(),
      )
    } catch (error) {
      settleJob(job, 'failed', { code: 'prompt_rejected', message: messageOf(error) })
      fail(error)
    }
    void checkJob(job)
    await awaitJob(job, waitMs)
    return { ...jobView(job), permission: permission ?? ctx.permissionPresets.current(session) }
  }

  function normalizeWait(value) {
    const parsed = typeof value === 'number' ? value : Number(value ?? DEFAULT_WAIT_MS)
    if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_WAIT_MS
    return Math.min(Math.trunc(parsed), MAX_WAIT_MS)
  }

  async function waitJob(jobId, url) {
    const job = await getJob(jobId)
    if (job === undefined) throw new HttpError(404, 'job_not_found', `job "${jobId}" is not persisted in this process`)
    if (!job.settled) {
      // `input-required` with no pending request left means the answer already
      // landed and the projection has not caught up; sample once before waiting.
      if (job.state === 'input-required' && pendingForSession(job.sessionId).length === 0) {
        await checkJob(job)
      } else {
        void checkJob(job)
      }
    }
    const waitMs = normalizeWait(url.searchParams.get('waitMs'))
    await awaitJob(job, waitMs)
    return jobView(job)
  }

  async function cancelSession(sessionId, jobId) {
    const job = jobId === undefined ? findActiveJob(sessionId) : await getJob(jobId)
    if (jobId !== undefined && job === undefined) {
      throw new HttpError(404, 'job_not_found', `job "${jobId}" is not persisted in this process`)
    }
    try {
      const result = await ctx.sessionController.cancel({ sessionId })
      if (job !== undefined && !job.settled) {
        job.state = 'cancelling'
        notifyJob(job)
        job.cancelTimer = setTimeout(() => {
          if (!job.settled) settleJob(job, 'cancelled', { code: 'cancelled', message: 'The turn was cancelled.' })
        }, 10_000)
        job.cancelTimer.unref?.()
      }
      return { ...(job === undefined ? {} : jobView(job)), accepted: result.accepted === true }
    } catch (error) {
      fail(error)
    }
  }

  async function listPresets() {
    const permissions = ctx.permissionPresets.catalog()
    const registry = ctx.get('agentPresets')
    let agentPresets = []
    if (registry !== undefined) {
      try {
        agentPresets = (await registry.list()).map((preset) => ({
          id: preset.id,
          name: preset.name ?? preset.id,
          broken: preset.broken === true,
        }))
      } catch {
        agentPresets = []
      }
    }
    return { permissions, agentPresets }
  }

  // ----------------------------------------------------------------- handler

  /** @param {import('node:http').IncomingMessage} req @param {import('node:http').ServerResponse} res */
  async function handler(req, res) {
    try {
      if (typeof req.headers.origin === 'string' && req.headers.origin !== '') {
        throw new HttpError(403, 'origin_forbidden', 'Browser-origin requests are not accepted by the delegate bridge.')
      }
      const authorization = req.headers.authorization
      if (typeof authorization !== 'string' || !authorization.startsWith('Bearer ')) {
        throw new HttpError(401, 'unauthorized', 'A bearer token is required.')
      }
      const presented = Buffer.from(authorization.slice('Bearer '.length))
      const expected = Buffer.from(token)
      if (presented.length !== expected.length || !timingSafeEqual(presented, expected)) {
        throw new HttpError(401, 'unauthorized', 'Invalid bearer token.')
      }

      const url = new URL(req.url ?? '/', 'http://127.0.0.1')
      const route = url.pathname.startsWith(ROUTE_PREFIX) ? url.pathname.slice(ROUTE_PREFIX.length) || '/' : url.pathname
      const method = req.method ?? 'GET'
      const callerHeader = req.headers['x-delegate-caller']
      const caller = typeof callerHeader === 'string' && callerHeader.trim() !== '' ? callerHeader.trim().slice(0, 120) : 'unknown'

      if (method === 'GET' && route === '/health') {
        sendJson(res, 200, {
          ok: true,
          plugin: 'dsh-delegate',
          version: PLUGIN_VERSION,
          pid: process.pid,
          routePrefix: ROUTE_PREFIX,
          initialisedAt: startedAt,
          jobs: jobs.size,
          workspaces: ctx.workspaceRegistry.list().length,
          pending: pendingRequests.size,
          eventClient: eventClient.status,
        })
        return
      }

      if (method === 'GET' && route === '/workspaces') {
        sendJson(res, 200, await listWorkspaces())
        return
      }

      if (method === 'POST' && route === '/workspaces') {
        sendJson(res, 201, { ...(await createWorkspace(await readJsonBody(req))), ok: true })
        return
      }

      if (method === 'GET' && route === '/sessions') {
        sendJson(res, 200, await listSessions(url))
        return
      }

      if (method === 'POST' && route === '/sessions') {
        sendJson(res, 201, { ...(await createSession(await readJsonBody(req))), ok: true })
        return
      }

      if (method === 'GET' && route === '/presets') {
        sendJson(res, 200, await listPresets())
        return
      }

      const pendingMatch = /^\/sessions\/([^/]+)\/pending(?:\/([^/]+)\/answer)?$/.exec(route)
      if (pendingMatch !== null) {
        const sessionId = decodeURIComponent(pendingMatch[1])
        const requestId = pendingMatch[2] === undefined ? undefined : decodeURIComponent(pendingMatch[2])
        if (requestId === undefined && method === 'GET') {
          sendJson(res, 200, await listPending(sessionId))
          return
        }
        if (requestId !== undefined && method === 'POST') {
          const body = await readJsonBody(req)
          sendJson(res, 200, { ...(await answerPending(sessionId, requestId, body, caller)), ok: true })
          return
        }
      }

      const sessionMatch = /^\/sessions\/([^/]+)(?:\/(prompt|cancel|state|last))?$/.exec(route)
      if (sessionMatch !== null) {
        const sessionId = decodeURIComponent(sessionMatch[1])
        const action = sessionMatch[2]
        if (action === undefined && method === 'GET') {
          sendJson(res, 200, await readSessionState(sessionId))
          return
        }
        if (action === 'prompt' && method === 'POST') {
          sendJson(res, 202, { ...(await promptSession(sessionId, await readJsonBody(req))), ok: true })
          return
        }
        if (action === 'cancel' && method === 'POST') {
          sendJson(res, 200, await cancelSession(sessionId, undefined))
          return
        }
        if (action === 'last' && method === 'GET') {
          sendJson(res, 200, await lastTurns(sessionId, url))
          return
        }
      }

      const jobMatch = /^\/jobs\/([^/]+)(?:\/cancel)?$/.exec(route)
      if (jobMatch !== null) {
        const jobId = decodeURIComponent(jobMatch[1])
        if (method === 'GET') {
          sendJson(res, 200, await waitJob(jobId, url))
          return
        }
        if (method === 'POST' && route.endsWith('/cancel')) {
          const job = await getJob(jobId)
          if (job === undefined) throw new HttpError(404, 'job_not_found', `job "${jobId}" is not persisted in this process`)
          sendJson(res, 200, await cancelSession(job.sessionId, job.id))
          return
        }
      }

      throw new HttpError(404, 'not_found', `No delegate route for ${method} ${url.pathname}`)
    } catch (error) {
      if (res.headersSent) {
        try {
          res.end()
        } catch {
          // already closed
        }
        return
      }
      if (error instanceof HttpError) {
        sendJson(res, error.status, { ok: false, error: { code: error.code, message: error.message } })
        return
      }
      sendJson(res, 500, { ok: false, error: { code: 'internal', message: messageOf(error) } })
    }
  }

  // ------------------------------------------------------------------ effects

  ctx.effect(() => {
    const disposeRoute = ctx.webServer.register({ kind: 'prefix', path: ROUTE_PREFIX, handler })
    void publishDiscovery()
    startEventClient()
    void (async () => {
      await store.init()
      await store.prune()
      for (const record of await store.loadAll()) {
        if (record?.settled === true) continue
        try {
          await recoverRecord(record)
        } catch {
          // One unreadable record must not stop the bridge.
        }
      }
    })()
    return () => {
      disposeRoute()
      stopEventClient()
      for (const job of jobs.values()) {
        job.settled = true
        disposeJobListeners(job)
      }
      jobs.clear()
      activeJobs.clear()
      void removeDiscovery()
    }
  }, 'dsh-delegate: bridge routes')
}
