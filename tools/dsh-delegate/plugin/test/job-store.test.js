import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { JobStore, fromRecord, toRecord } from '../lib/job-store.js'

const JOB_ID = '11111111-2222-3333-4444-555555555555'

function sampleJob(overrides = {}) {
  return {
    id: JOB_ID,
    sessionId: 'session-abc',
    requestId: 'request-abc',
    afterSeq: 41,
    state: 'running',
    text: '',
    turnEnd: null,
    error: null,
    pendingInput: [],
    createdAt: 1_000,
    updatedAt: 2_000,
    expireAt: 3_000,
    suspended: false,
    settled: false,
    ...overrides,
  }
}

test('persists and reloads a job view with terminal text', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-delegate-store-'))
  try {
    const store = new JobStore(dir)
    await store.init()
    await store.save(toRecord(sampleJob({ state: 'done', text: 'final answer', settled: true })))
    const loaded = await store.load(JOB_ID)
    assert.equal(loaded.text, 'final answer')
    assert.equal(loaded.settled, true)
    const mode = (await (await import('node:fs/promises')).stat(store.filePath(JOB_ID))).mode & 0o777
    assert.equal(mode, 0o600)
    const view = fromRecord(loaded)
    assert.equal(view.state, 'done')
    assert.equal(view.text, 'final answer')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('loadAll ignores foreign and torn files, prune drops old settled records', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'dsh-delegate-store-'))
  try {
    const store = new JobStore(dir)
    await store.init()
    await store.save(toRecord(sampleJob({ settled: true, state: 'done', updatedAt: 1_000 })))
    await store.save(toRecord(sampleJob({ id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', updatedAt: 9_999 })))
    await (await import('node:fs/promises')).writeFile(join(dir, 'notes.txt'), 'ignore me')
    const records = await store.loadAll()
    assert.equal(records.length, 2)
    await store.prune(100_000_000, 24 * 60 * 60 * 1000)
    const after = await store.loadAll()
    assert.deepEqual(
      after.map((record) => record.jobId),
      ['aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'],
    )
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('rejects path traversal in job ids', async () => {
  const store = new JobStore(tmpdir())
  assert.throws(() => store.filePath('../escape'), /invalid job id/)
  assert.throws(() => store.filePath('not-a-uuid'), /invalid job id/)
})

test('record round-trip preserves pending input and turn ends', async () => {
  const job = sampleJob({
    state: 'input-required',
    pendingInput: ['approval'],
    turnEnd: { reason: { kind: 'aborted' }, turn: 3, seq: 77 },
    extensions: 4,
    lastObserved: 'running',
  })
  const record = toRecord(job)
  const restored = fromRecord(record)
  assert.deepEqual(restored.pendingInput, ['approval'])
  assert.equal(restored.extensions, 4)
  assert.equal(restored.lastObserved, 'running')
  assert.equal(restored.turnEnd.turn, 3)
  assert.equal(restored.id, JOB_ID)
})
