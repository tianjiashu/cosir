import assert from 'node:assert/strict'
import test from 'node:test'

import {
  answerLogEntry,
  normalizeApprovalDecision,
  parseEscalationScope,
  pendingSummaryLine,
  summarizePendingFrame,
  validateQuestionAnswer,
} from '../lib/pending-requests.js'

const APPROVAL_FRAME = {
  type: 'waterfall',
  event: 'approval/request',
  eventId: 'evt-1',
  agentId: 'session-1',
  request: {
    toolName: 'bash',
    callId: 'call-1',
    reason: 'escalate sandbox to danger-full-access: writing outside the workspace',
    displayReason: { en: 'Use danger-full-access for this call' },
  },
}

const QUESTION_FRAME = {
  type: 'waterfall',
  event: 'user-questions/request',
  eventId: 'evt-2',
  agentId: 'session-1',
  request: {
    questions: [
      {
        id: 'q1',
        question: 'Deploy now?',
        header: 'Deploy',
        multiSelect: false,
        options: [{ label: 'Yes', description: 'ship it' }, { label: 'No' }],
      },
    ],
    wait: { callId: 'call-2', timed: false },
  },
}

test('approval frames summarize with escalation scope and tool identity', () => {
  const summary = summarizePendingFrame(APPROVAL_FRAME, 123)
  assert.equal(summary.requestId, 'evt-1')
  assert.equal(summary.kind, 'approval')
  assert.equal(summary.sessionId, 'session-1')
  assert.equal(summary.toolName, 'bash')
  assert.equal(summary.callId, 'call-1')
  assert.equal(summary.requestedScope, 'danger-full-access')
  assert.equal(summary.receivedAt, 123)
})

test('escalation scope parsing tolerates other reasons', () => {
  assert.equal(parseEscalationScope('escalate sandbox to workspace-write: need repo writes'), 'workspace-write')
  assert.equal(parseEscalationScope('plain reason'), null)
  assert.equal(parseEscalationScope(undefined), null)
})

test('question frames summarize normalized questions and options', () => {
  const summary = summarizePendingFrame(QUESTION_FRAME, 5)
  assert.equal(summary.kind, 'question')
  assert.equal(summary.callId, 'call-2')
  assert.equal(summary.questions.length, 1)
  assert.equal(summary.questions[0].id, 'q1')
  assert.equal(summary.questions[0].options[1].label, 'No')
  assert.equal(summary.receivedAt, 5)
})

test('non-waterfall and unknown-event frames are ignored', () => {
  assert.equal(summarizePendingFrame({ type: 'emit', event: 'approval/request' }), undefined)
  assert.equal(summarizePendingFrame({ type: 'waterfall', event: 'other/event', eventId: 'x', agentId: 's' }), undefined)
  assert.equal(summarizePendingFrame(undefined), undefined)
})

test('approval decisions normalize to the DSH vocabulary only', () => {
  assert.equal(normalizeApprovalDecision('allow-once'), 'allowed-once')
  assert.equal(normalizeApprovalDecision('allowed-once'), 'allowed-once')
  assert.equal(normalizeApprovalDecision('allow'), 'allowed-once')
  assert.equal(normalizeApprovalDecision('reject'), 'rejected')
  assert.equal(normalizeApprovalDecision('deny'), 'rejected')
  assert.equal(normalizeApprovalDecision('always'), undefined)
  assert.equal(normalizeApprovalDecision(undefined), undefined)
})

test('question answers validate the batch shape', () => {
  assert.deepEqual(validateQuestionAnswer({ answers: [{ id: 'q1', selected: ['Yes'] }] }), {
    ok: true,
    answers: [{ id: 'q1', selected: ['Yes'] }],
  })
  assert.deepEqual(validateQuestionAnswer({ answers: [{ id: 'q1', selected: [], custom: 'later' }] }), {
    ok: true,
    answers: [{ id: 'q1', selected: [], custom: 'later' }],
  })
  assert.equal(validateQuestionAnswer({}).ok, false)
  assert.equal(validateQuestionAnswer({ answers: [] }).ok, false)
  assert.equal(validateQuestionAnswer({ answers: [{ id: 1, selected: [] }] }).ok, false)
  assert.equal(validateQuestionAnswer({ answers: [{ id: 'q1', selected: [2] }] }).ok, false)
})

test('pending lines read well for both kinds', () => {
  const approval = summarizePendingFrame(APPROVAL_FRAME)
  assert.match(pendingSummaryLine(approval), /approval evt-1 tool=bash/)
  assert.match(pendingSummaryLine(approval), /danger-full-access/)
  const question = summarizePendingFrame(QUESTION_FRAME)
  assert.match(pendingSummaryLine(question), /question evt-2 Deploy now\?/)
})

test('answer audit entries carry caller, decision, and request summary', () => {
  const approval = summarizePendingFrame(APPROVAL_FRAME)
  const job = { id: 'job-1', sessionId: 'session-1' }
  const entry = answerLogEntry({ job, entry: approval, decision: 'allowed-once', caller: 'claude', reason: 'tests only' })
  assert.equal(entry.event, 'delegate.job.approval-answered')
  assert.equal(entry.jobId, 'job-1')
  assert.equal(entry.sessionId, 'session-1')
  assert.equal(entry.requestId, 'evt-1')
  assert.equal(entry.requestKind, 'approval')
  assert.equal(entry.decision, 'allowed-once')
  assert.equal(entry.caller, 'claude')
  assert.equal(entry.reason, 'tests only')
  assert.equal(entry.requestSummary.requestedScope, 'danger-full-access')

  const question = summarizePendingFrame(QUESTION_FRAME)
  const questionEntry = answerLogEntry({
    job: undefined,
    entry: question,
    decision: 'answered',
    caller: 'claude',
    reason: undefined,
  })
  assert.equal(questionEntry.jobId, undefined)
  assert.equal(questionEntry.requestKind, 'question')
  assert.equal(questionEntry.requestSummary.questions.length, 1)
  assert.equal('reason' in questionEntry, false)
})
