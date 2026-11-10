/**
 * Pure job policy for the delegate bridge: expiry decisions, turn-end
 * settlement, assistant-text extraction, and duration normalization.
 *
 * Kept free of Cordis/Node service access so the decision table can be unit
 * tested without a running Harness host.
 *
 * Expiry is an observation window, not a job lifetime:
 *  - a running / waiting-for-human / parked session extends the window;
 *  - a deterministically gone session settles `lost` immediately;
 *  - an unreadable session state may extend, but not forever
 *    ({@link MAX_UNREADABLE_STREAK} consecutive reads or
 *    {@link MAX_UNREADABLE_MS} total);
 *  - past `expireMs + HARD_EXTENSION_MS` the job always settles `expired`,
 *    whatever the session is doing, so a caller's `--until-done` can end.
 */

/** Default observation window for one delegated job (12 hours). */
export const DEFAULT_EXPIRE_MS = 12 * 60 * 60 * 1000

/** Upper bound a caller may request for one job's observation window (24 hours). */
export const MAX_EXPIRE_MS = 24 * 60 * 60 * 1000

/** How long a settled job's persisted view is retained on disk (24 hours). */
export const TERMINAL_RETAIN_MS = 24 * 60 * 60 * 1000

/** Hard extension allowed after the observation window, regardless of session state. */
export const HARD_EXTENSION_MS = 2 * 60 * 60 * 1000

/** Consecutive unreadable session-state samples that settle the job `lost`. */
export const MAX_UNREADABLE_STREAK = 3

/** Total time a job may spend on unreadable session state before settling `lost`. */
export const MAX_UNREADABLE_MS = 30 * 60 * 1000

/** Longest single expiry-extension step; running sessions are rechecked this often. */
const MAX_GRACE_MS = 30 * 60 * 1000

/** @param {unknown} value */
export function normalizeExpireMs(value) {
  if (value === undefined || value === null || value === '') return DEFAULT_EXPIRE_MS
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_EXPIRE_MS
  return Math.min(Math.max(Math.trunc(parsed), 1000), MAX_EXPIRE_MS)
}

/** @param {number} expireMs */
export function graceMsFor(expireMs) {
  return Math.max(1000, Math.min(expireMs, MAX_GRACE_MS))
}

/** @param {any} message */
export function textOfAssistantMessage(message) {
  const content = message?.content
  if (!Array.isArray(content)) return ''
  return content
    .flatMap((block) => (block?.type === 'text' && typeof block.text === 'string' ? [block.text] : []))
    .join('')
}

/**
 * Map a DSH `turn/end` reason to a terminal job outcome.
 * @param {any} reason
 * @returns {{ state: string, error: { code: string, message: string } | null }}
 */
export function settleFromTurnEnd(reason) {
  const kind = reason?.kind
  if (kind === 'completed' || kind === 'max-tokens') {
    return { state: 'done', error: null }
  }
  if (kind === 'aborted') {
    return { state: 'cancelled', error: { code: 'cancelled', message: 'The turn was cancelled.' } }
  }
  if (kind === 'interrupted') {
    return { state: 'cancelled', error: { code: 'interrupted', message: 'The turn was interrupted.' } }
  }
  if (kind === 'error') {
    return {
      state: 'failed',
      error: { code: 'agent_error', message: reason?.error?.message ?? 'The agent run failed.' },
    }
  }
  if (kind === 'blocked') {
    return { state: 'failed', error: { code: 'agent_blocked', message: 'The agent run was blocked.' } }
  }
  return { state: 'done', error: null }
}

/**
 * Classify one readable passive state sample. Pending input wins over
 * `running`: a turn that is blocked on an approval registers
 * `pendingInput` while the agent status is still `running`.
 * @param {{ running?: boolean, pendingInput?: readonly string[] } | undefined} sessionState
 * @returns {'input-required' | 'running' | 'idle'}
 */
export function classifySessionState(sessionState) {
  const pending = Array.isArray(sessionState?.pendingInput) ? sessionState.pendingInput : []
  if (pending.length > 0) return 'input-required'
  if (sessionState?.running === true) return 'running'
  return 'idle'
}

/**
 * Structured log entry for a job state flip (input-required / resumed).
 * @param {any} job
 * @param {string} event
 */
export function transitionLogEntry(job, event) {
  return {
    event,
    jobId: job.id,
    sessionId: job.sessionId,
    extensions: job.extensions ?? 0,
    elapsedMs: Date.now() - job.createdAt,
    lastObserved: job.lastObserved ?? null,
    pendingInput: Array.isArray(job.pendingInput) ? job.pendingInput : [],
  }
}

/**
 * Whether a session-state read error proves the session is gone for good.
 * A missing Session is definitive; timeouts and internal failures are not.
 * @param {{ code?: string, message?: string } | undefined} readError
 */
export function isDefinitiveSessionLoss(readError) {
  const code = readError?.code
  return code === 'session/not-found' || code === 'deleted' || code === 'session/deleted'
}

/**
 * Decide what a spent observation window means for a job.
 *
 * @param {object} input
 * @param {boolean} input.hasTurnEnd
 * @param {any} [input.sessionState] current passive state when readable
 * @param {{ code?: string, message?: string }} [input.readError] why the state was unreadable
 * @param {boolean} [input.suspended]
 * @param {number} input.createdAt job creation time (ms)
 * @param {number} input.now current time (ms)
 * @param {number} input.expireMs original observation window
 * @param {number} [input.hardExtensionMs] hard extension after the window
 * @param {number} [input.extensions] extensions already granted
 * @param {number} [input.unreadableStreak] consecutive unreadable samples including the current one
 * @param {number | null} [input.unreadableSince] first unreadable sample time
 * @param {string | null} [input.lastObserved] last session state seen
 * @returns {{ action: 'settle', state: string, error: any } | { action: 'extend', reason: string }}
 */
export function decideExpiry({
  hasTurnEnd,
  sessionState,
  readError,
  suspended,
  createdAt,
  now,
  expireMs,
  hardExtensionMs = HARD_EXTENSION_MS,
  extensions = 0,
  unreadableStreak = 0,
  unreadableSince = null,
  lastObserved = null,
}) {
  if (hasTurnEnd) {
    return { action: 'settle', state: 'internal-turn-end', error: null }
  }

  const hardDeadline = createdAt + expireMs + hardExtensionMs
  const pastHardDeadline = now >= hardDeadline
  const expiredDecision = (observed) => ({
    action: 'settle',
    state: 'expired',
    error: {
      code: 'expired',
      message:
        `The job exceeded its observation window (${expireMs} ms + ${hardExtensionMs} ms hard extension) ` +
        `after ${extensions} extension(s); last observed session state: ${observed ?? lastObserved ?? 'unknown'}. ` +
        'Read existing output with "last --session", or inspect the turn in the Harness GUI.',
    },
  })

  // 1) Definitive loss: the session no longer exists. Never extend.
  if (readError !== undefined && isDefinitiveSessionLoss(readError)) {
    return {
      action: 'settle',
      state: 'lost',
      error: {
        code: 'session-gone',
        message: `The Session is no longer available (${readError.code}). Use "last --session" to read any persisted answer.`,
      },
    }
  }

  // 2) Unreadable state: extend, but only within the unreadable budget.
  if (sessionState === undefined || sessionState === null) {
    const streak = unreadableStreak
    const streakExhausted = streak >= MAX_UNREADABLE_STREAK
    const durationExhausted = unreadableSince !== null && now - unreadableSince >= MAX_UNREADABLE_MS
    if (streakExhausted || durationExhausted) {
      return {
        action: 'settle',
        state: 'lost',
        error: {
          code: 'state-unreadable',
          message:
            `The Session state could not be read ${streak} consecutive time(s) over ` +
            `${unreadableSince === null ? 'an unknown duration' : `${now - unreadableSince} ms`}; ` +
            `last error: ${readError?.message ?? 'unknown'}. Use "last --session" to read any persisted answer.`,
        },
      }
    }
    if (pastHardDeadline) return expiredDecision(null)
    return { action: 'extend', reason: 'state-unreadable' }
  }

  // 3) Readable: running / waiting for a human / parked extend the window.
  const pending = Array.isArray(sessionState.pendingInput) ? sessionState.pendingInput : []
  const observed =
    sessionState.running === true
      ? 'running'
      : pending.length > 0
        ? `input-required(${pending.join(',')})`
        : suspended === true
          ? 'suspended'
          : 'idle'
  if (observed !== 'idle') {
    if (pastHardDeadline) return expiredDecision(observed)
    return { action: 'extend', reason: observed }
  }

  // 4) Idle with nothing left to observe: the turn end was lost.
  return {
    action: 'settle',
    state: 'lost',
    error: {
      code: 'lost',
      message:
        'The session went idle without reporting a turn end for this job; the final answer could not be observed. ' +
        'Read the session in the Harness GUI or retry with "last".',
    },
  }
}

/**
 * Structured log entry for one granted extension. The bridge serializes this
 * to one JSON line so "why did this job keep extending?" is answerable later.
 * @param {any} job
 * @param {string} reason
 */
export function extensionLogEntry(job, reason) {
  return {
    event: 'delegate.job.extend',
    jobId: job.id,
    sessionId: job.sessionId,
    reason,
    extensions: job.extensions + 1,
    elapsedMs: Date.now() - job.createdAt,
    expireMs: job.expireDuration,
    lastObserved: job.lastObserved ?? null,
    unreadableStreak: job.unreadableStreak ?? 0,
  }
}

/**
 * Structured log entry for a terminal settlement.
 * @param {any} job
 * @param {string} state
 * @param {{ code?: string } | null} error
 */
export function settlementLogEntry(job, state, error) {
  return {
    event: 'delegate.job.settle',
    jobId: job.id,
    sessionId: job.sessionId,
    state,
    errorCode: error?.code ?? null,
    extensions: job.extensions ?? 0,
    elapsedMs: Date.now() - job.createdAt,
    lastObserved: job.lastObserved ?? null,
  }
}
