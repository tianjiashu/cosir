import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_EXPIRE_MS,
  HARD_EXTENSION_MS,
  MAX_EXPIRE_MS,
  MAX_UNREADABLE_MS,
  MAX_UNREADABLE_STREAK,
  classifySessionState,
  decideExpiry,
  extensionLogEntry,
  graceMsFor,
  isDefinitiveSessionLoss,
  normalizeExpireMs,
  settleFromTurnEnd,
  settlementLogEntry,
  textOfAssistantMessage,
  transitionLogEntry,
} from '../lib/job-policy.js'

const BASE = {
  hasTurnEnd: false,
  createdAt: 1_000_000,
  now: 1_030_000,
  expireMs: 60_000,
  hardExtensionMs: HARD_EXTENSION_MS,
}
const HARD_DEADLINE = BASE.createdAt + BASE.expireMs + BASE.hardExtensionMs

test('normalizeExpireMs defaults, clamps, and rejects garbage', () => {
  assert.equal(normalizeExpireMs(undefined), DEFAULT_EXPIRE_MS)
  assert.equal(normalizeExpireMs(''), DEFAULT_EXPIRE_MS)
  assert.equal(normalizeExpireMs('nope'), DEFAULT_EXPIRE_MS)
  assert.equal(normalizeExpireMs(-5), DEFAULT_EXPIRE_MS)
  assert.equal(normalizeExpireMs(5), 1000)
  assert.equal(normalizeExpireMs(8 * 60 * 60 * 1000), 8 * 60 * 60 * 1000)
  assert.equal(normalizeExpireMs(48 * 60 * 60 * 1000), MAX_EXPIRE_MS)
})

test('grace step is bounded between one second and thirty minutes', () => {
  assert.equal(graceMsFor(3000), 3000)
  assert.equal(graceMsFor(60_000), 60_000)
  assert.equal(graceMsFor(8 * 60 * 60 * 1000), 30 * 60 * 1000)
})

test('assistant text joins only text blocks', () => {
  assert.equal(
    textOfAssistantMessage({
      content: [
        { type: 'text', text: 'hello ' },
        { type: 'tool-call', id: 'c1' },
        { type: 'text', text: 'world' },
      ],
    }),
    'hello world',
  )
  assert.equal(textOfAssistantMessage(undefined), '')
})

test('turn-end reasons map to terminal outcomes', () => {
  assert.deepEqual(settleFromTurnEnd({ kind: 'completed' }), { state: 'done', error: null })
  assert.deepEqual(settleFromTurnEnd({ kind: 'max-tokens' }), { state: 'done', error: null })
  assert.equal(settleFromTurnEnd({ kind: 'aborted' }).state, 'cancelled')
  assert.equal(settleFromTurnEnd({ kind: 'interrupted' }).state, 'cancelled')
  assert.equal(settleFromTurnEnd({ kind: 'error', error: { message: 'boom' } }).error.message, 'boom')
  assert.equal(settleFromTurnEnd({ kind: 'blocked' }).state, 'failed')
})

test('pending input wins over running for state classification', () => {
  assert.equal(classifySessionState({ running: true, pendingInput: ['approval'] }), 'input-required')
  assert.equal(classifySessionState({ running: true, pendingInput: ['question'] }), 'input-required')
  assert.equal(classifySessionState({ running: false, pendingInput: ['approval'] }), 'input-required')
})

test('clearing pending input returns the job to running', () => {
  assert.equal(classifySessionState({ running: true, pendingInput: [] }), 'running')
  const flip = [classifySessionState({ running: true, pendingInput: ['approval'] }), classifySessionState({ running: true, pendingInput: [] })]
  assert.deepEqual(flip, ['input-required', 'running'])
})

test('idle classification covers stopped and empty sessions', () => {
  assert.equal(classifySessionState({ running: false, pendingInput: [] }), 'idle')
  assert.equal(classifySessionState({}), 'idle')
  assert.equal(classifySessionState(undefined), 'idle')
})

test('transition log entries name the flip and carry diagnostics', () => {
  const job = {
    id: 'job-1',
    sessionId: 'session-1',
    createdAt: Date.now() - 2_000,
    extensions: 1,
    lastObserved: 'running',
    pendingInput: ['approval'],
  }
  const entry = transitionLogEntry(job, 'delegate.job.input-required')
  assert.equal(entry.event, 'delegate.job.input-required')
  assert.equal(entry.jobId, 'job-1')
  assert.equal(entry.extensions, 1)
  assert.deepEqual(entry.pendingInput, ['approval'])
  assert.equal(transitionLogEntry(job, 'delegate.job.resumed').event, 'delegate.job.resumed')
})

test('definitive session loss is exactly not-found/deleted', () => {
  assert.equal(isDefinitiveSessionLoss({ code: 'session/not-found' }), true)
  assert.equal(isDefinitiveSessionLoss({ code: 'deleted' }), true)
  assert.equal(isDefinitiveSessionLoss({ code: 'gateway/internal' }), false)
  assert.equal(isDefinitiveSessionLoss({ code: 'session/writer-held' }), false)
  assert.equal(isDefinitiveSessionLoss(undefined), false)
})

test('a missing session settles lost immediately, never extends', () => {
  const decision = decideExpiry({ ...BASE, readError: { code: 'session/not-found', message: 'gone' } })
  assert.equal(decision.action, 'settle')
  assert.equal(decision.state, 'lost')
  assert.equal(decision.error.code, 'session-gone')
})

test('a temporarily unreadable state keeps extending below the streak threshold', () => {
  for (const streak of [1, MAX_UNREADABLE_STREAK - 1]) {
    const decision = decideExpiry({
      ...BASE,
      sessionState: undefined,
      readError: { code: 'gateway/internal', message: 'timeout' },
      unreadableStreak: streak,
      unreadableSince: BASE.now - 60_000,
    })
    assert.equal(decision.action, 'extend', `streak ${streak} should extend`)
    assert.equal(decision.reason, 'state-unreadable')
  }
})

test('consecutive unreadable samples at the threshold settle lost with the last error', () => {
  const decision = decideExpiry({
    ...BASE,
    sessionState: undefined,
    readError: { code: 'gateway/internal', message: 'db is down' },
    unreadableStreak: MAX_UNREADABLE_STREAK,
    unreadableSince: BASE.now - 60_000,
  })
  assert.equal(decision.action, 'settle')
  assert.equal(decision.state, 'lost')
  assert.equal(decision.error.code, 'state-unreadable')
  assert.match(decision.error.message, /db is down/)
})

test('unreadable state over the duration budget settles lost', () => {
  const decision = decideExpiry({
    ...BASE,
    sessionState: undefined,
    readError: { code: 'timeout', message: 'slow' },
    unreadableStreak: 1,
    unreadableSince: BASE.now - MAX_UNREADABLE_MS,
  })
  assert.equal(decision.action, 'settle')
  assert.equal(decision.state, 'lost')
  assert.equal(decision.error.code, 'state-unreadable')
})

test('a running session extends before the hard deadline', () => {
  const decision = decideExpiry({ ...BASE, sessionState: { running: true } })
  assert.deepEqual(decision, { action: 'extend', reason: 'running' })
})

test('a running session past the hard deadline settles expired with the last observed state', () => {
  const decision = decideExpiry({ ...BASE, now: HARD_DEADLINE, sessionState: { running: true } })
  assert.equal(decision.action, 'settle')
  assert.equal(decision.state, 'expired')
  assert.equal(decision.error.code, 'expired')
  assert.match(decision.error.message, /last observed session state: running/)
})

test('pending input past the hard deadline reports input-required detail', () => {
  const decision = decideExpiry({
    ...BASE,
    now: HARD_DEADLINE + 1,
    sessionState: { running: false, pendingInput: ['approval', 'question'] },
  })
  assert.equal(decision.state, 'expired')
  assert.match(decision.error.message, /input-required\(approval,question\)/)
})

test('a parked session extends, and expires past the hard deadline', () => {
  const extend = decideExpiry({ ...BASE, sessionState: { running: false, pendingInput: [] }, suspended: true })
  assert.deepEqual(extend, { action: 'extend', reason: 'suspended' })
  const expired = decideExpiry({
    ...BASE,
    now: HARD_DEADLINE,
    sessionState: { running: false, pendingInput: [] },
    suspended: true,
  })
  assert.equal(expired.state, 'expired')
  assert.match(expired.error.message, /last observed session state: suspended/)
})

test('an idle session with no turn end settles lost', () => {
  const decision = decideExpiry({ ...BASE, sessionState: { running: false, pendingInput: [] } })
  assert.equal(decision.action, 'settle')
  assert.equal(decision.state, 'lost')
  assert.equal(decision.error.code, 'lost')
})

test('unreadable state still respects the hard deadline', () => {
  const decision = decideExpiry({
    ...BASE,
    now: HARD_DEADLINE,
    sessionState: undefined,
    readError: { code: 'gateway/internal', message: 'boom' },
    unreadableStreak: 1,
    unreadableSince: HARD_DEADLINE - 1000,
  })
  assert.equal(decision.state, 'expired')
  assert.equal(decision.error.code, 'expired')
})

test('an observed turn end settles instead of expiring', () => {
  const decision = decideExpiry({ ...BASE, hasTurnEnd: true, sessionState: { running: true } })
  assert.equal(decision.action, 'settle')
})

test('extension log entries carry diagnostics', () => {
  const job = {
    id: 'job-1',
    sessionId: 'session-1',
    createdAt: Date.now() - 5_000,
    extensions: 2,
    expireDuration: 60_000,
    lastObserved: 'running',
    unreadableStreak: 0,
  }
  const entry = extensionLogEntry(job, 'running')
  assert.equal(entry.event, 'delegate.job.extend')
  assert.equal(entry.jobId, 'job-1')
  assert.equal(entry.extensions, 3)
  assert.equal(entry.reason, 'running')
  assert.equal(entry.lastObserved, 'running')
  assert.ok(entry.elapsedMs >= 5_000)
})

test('settlement log entries carry diagnostics', () => {
  const job = { id: 'job-1', sessionId: 'session-1', createdAt: Date.now() - 1_000, extensions: 4, lastObserved: 'suspended' }
  const entry = settlementLogEntry(job, 'expired', { code: 'expired' })
  assert.equal(entry.event, 'delegate.job.settle')
  assert.equal(entry.state, 'expired')
  assert.equal(entry.errorCode, 'expired')
  assert.equal(entry.extensions, 4)
  assert.equal(entry.lastObserved, 'suspended')
})
