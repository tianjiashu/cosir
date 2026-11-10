/**
 * Pure helpers for the delegate bridge's answering client.
 *
 * The GUI answers approvals and questions as a forwarded `$events` client:
 * Cordis waterfalls (`approval/request`, `user-questions/request`) are projected
 * to the browser, and the browser replies through `$events/result`. The bridge
 * joins as its own owner answering Peer, so both the GUI and the bridge see the
 * same request; whichever replies first settles it and the gateway cancels the
 * other delivery (which is what closes the GUI card).
 *
 * Nothing here touches Cordis: summaries, decision validation and audit entries
 * are plain data so they can be unit tested.
 */

/** @param {unknown} value */
function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/**
 * Parse the escalation target out of an approval reason such as
 * `escalate sandbox to danger-full-access: writing outside the workspace`.
 * @param {unknown} reason
 * @returns {string | null}
 */
export function parseEscalationScope(reason) {
  if (typeof reason !== 'string') return null
  const match = /escalate sandbox to ([a-z][a-z-]*)/i.exec(reason)
  return match === null ? null : match[1]
}

/** @param {any} question */
function normalizeQuestion(question) {
  const options = Array.isArray(question?.options)
    ? question.options
        .filter((option) => isRecord(option) && typeof option.label === 'string')
        .map((option) => ({
          label: option.label,
          ...(typeof option.description === 'string' ? { description: option.description } : {}),
        }))
    : []
  return {
    id: typeof question?.id === 'string' ? question.id : null,
    question: typeof question?.question === 'string' ? question.question : null,
    ...(typeof question?.header === 'string' ? { header: question.header } : {}),
    ...(typeof question?.detail === 'string' ? { detail: question.detail } : {}),
    ...(question?.multiSelect === true ? { multiSelect: true } : {}),
    options,
  }
}

/**
 * Turn one forwarded waterfall frame into the request summary the bridge lists.
 * @param {any} frame validated `{type:'waterfall', event, eventId, agentId, request}` frame
 * @param {number} [receivedAt]
 * @returns {any | undefined}
 */
export function summarizePendingFrame(frame, receivedAt = Date.now()) {
  if (!isRecord(frame) || frame.type !== 'waterfall') return undefined
  if (typeof frame.eventId !== 'string' || typeof frame.agentId !== 'string') return undefined
  const request = isRecord(frame.request) ? frame.request : {}

  if (frame.event === 'approval/request') {
    return {
      requestId: frame.eventId,
      kind: 'approval',
      sessionId: frame.agentId,
      receivedAt,
      toolName: typeof request.toolName === 'string' ? request.toolName : null,
      callId: typeof request.callId === 'string' ? request.callId : null,
      reason: typeof request.reason === 'string' ? request.reason : null,
      displayReason: request.displayReason ?? null,
      requestedScope: parseEscalationScope(request.reason),
    }
  }

  if (frame.event === 'user-questions/request') {
    const questions = Array.isArray(request.questions) ? request.questions.map(normalizeQuestion) : []
    return {
      requestId: frame.eventId,
      kind: 'question',
      sessionId: frame.agentId,
      receivedAt,
      questions,
      callId: typeof request.wait?.callId === 'string' ? request.wait.callId : null,
    }
  }

  return undefined
}

/**
 * Canonical approval outcomes the bridge may submit. Rejection reasons are
 * audit-only: DSH's approval outcome vocabulary carries no free text.
 * @param {unknown} value
 * @returns {'allowed-once' | 'rejected' | undefined}
 */
export function normalizeApprovalDecision(value) {
  if (value === 'allow-once' || value === 'allowed-once' || value === 'allow') return 'allowed-once'
  if (value === 'rejected' || value === 'reject' || value === 'deny') return 'rejected'
  return undefined
}

/**
 * Validate a question answer value supplied by the caller.
 * @param {unknown} value
 * @returns {{ ok: true, answers: any[] } | { ok: false, message: string }}
 */
export function validateQuestionAnswer(value) {
  if (!isRecord(value) || !Array.isArray(value.answers)) {
    return { ok: false, message: 'answer must be an object with an "answers" array' }
  }
  const answers = []
  for (const item of value.answers) {
    if (!isRecord(item) || typeof item.id !== 'string' || !Array.isArray(item.selected)) {
      return { ok: false, message: 'each answer needs a string id and a selected array' }
    }
    if (!item.selected.every((label) => typeof label === 'string')) {
      return { ok: false, message: 'selected must contain option labels (strings)' }
    }
    answers.push({
      id: item.id,
      selected: [...item.selected],
      ...(typeof item.custom === 'string' && item.custom !== '' ? { custom: item.custom } : {}),
    })
  }
  if (answers.length === 0) return { ok: false, message: 'answers must contain at least one item' }
  return { ok: true, answers }
}

/** One-line human summary used by the client when a job waits on a human. */
export function pendingSummaryLine(entry) {
  if (entry?.kind === 'approval') {
    const scope = entry.requestedScope === null ? '' : ` esc=${entry.requestedScope}`
    const reason = typeof entry.reason === 'string' ? ` ${entry.reason}` : ''
    return `approval ${entry.requestId} tool=${entry.toolName ?? '?'}${scope}${reason}`.trim()
  }
  if (entry?.kind === 'question') {
    const first = Array.isArray(entry.questions) ? entry.questions[0] : undefined
    const text = typeof first?.question === 'string' ? ` ${first.question}` : ''
    return `question ${entry.requestId}${text}`.trim()
  }
  return `pending ${entry?.requestId ?? '?'}`
}

/**
 * Audit entry written to `events.jsonl` when the bridge answers a request.
 * @param {object} input
 * @param {any} input.job
 * @param {any} input.entry
 * @param {string} input.decision
 * @param {string} input.caller
 * @param {string | undefined} input.reason
 */
export function answerLogEntry({ job, entry, decision, caller, reason }) {
  return {
    event: 'delegate.job.approval-answered',
    ...(job === undefined ? {} : { jobId: job.id, sessionId: job.sessionId }),
    sessionId: entry.sessionId,
    requestId: entry.requestId,
    requestKind: entry.kind,
    decision,
    caller,
    ...(reason === undefined ? {} : { reason }),
    requestSummary:
      entry.kind === 'approval'
        ? {
            toolName: entry.toolName,
            callId: entry.callId,
            requestedScope: entry.requestedScope,
            reason: entry.reason,
          }
        : { callId: entry.callId, questions: entry.questions },
  }
}
