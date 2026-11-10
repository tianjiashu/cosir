/**
 * Durable job views for the delegate bridge.
 *
 * Each job's observable view (including the terminal answer) is written to
 * `$DSH_HOME/delegate-jobs/<jobId>.json` with mode 0600, so a bridge reload or
 * a desktop restart does not lose answers the caller has not collected yet.
 * Settled records are retained for {@link TERMINAL_RETAIN_MS}.
 */

import { mkdir, readFile, readdir, rename, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { TERMINAL_RETAIN_MS } from './job-policy.js'

const SAFE_JOB_ID = /^[0-9a-fA-F-]{36}$/

export class JobStore {
  /** @param {string} dir */
  constructor(dir) {
    this.dir = dir
  }

  async init() {
    await mkdir(this.dir, { recursive: true })
  }

  /** @param {string} jobId */
  filePath(jobId) {
    if (!SAFE_JOB_ID.test(jobId)) throw new Error(`invalid job id: ${jobId}`)
    return join(this.dir, `${jobId}.json`)
  }

  /** @param {{ jobId: string }} record */
  async save(record) {
    const target = this.filePath(record.jobId)
    const temporary = `${target}.tmp`
    await writeFile(temporary, `${JSON.stringify(record)}\n`, { mode: 0o600 })
    await rename(temporary, target)
  }

  /** @param {string} jobId */
  async load(jobId) {
    try {
      return JSON.parse(await readFile(this.filePath(jobId), 'utf8'))
    } catch {
      return undefined
    }
  }

  async loadAll() {
    let names
    try {
      names = await readdir(this.dir)
    } catch {
      return []
    }
    const records = []
    for (const name of names) {
      if (!name.endsWith('.json')) continue
      const jobId = name.slice(0, -'.json'.length)
      if (!SAFE_JOB_ID.test(jobId)) continue
      try {
        records.push(JSON.parse(await readFile(join(this.dir, name), 'utf8')))
      } catch {
        // A torn or foreign file must not stop recovery.
      }
    }
    return records
  }

  /** @param {string} jobId */
  async remove(jobId) {
    try {
      await unlink(this.filePath(jobId))
    } catch {
      // already gone
    }
  }

  /**
   * Drop settled records older than the retention window.
   * @param {number} [now]
   * @param {number} [retainMs]
   */
  async prune(now = Date.now(), retainMs = TERMINAL_RETAIN_MS) {
    for (const record of await this.loadAll()) {
      if (record?.settled === true && typeof record.updatedAt === 'number' && now - record.updatedAt > retainMs) {
        await this.remove(record.jobId)
      }
    }
  }
}

/** Build the persisted record for one in-memory job. @param {any} job */
export function toRecord(job) {
  return {
    version: 1,
    jobId: job.id,
    sessionId: job.sessionId,
    requestId: job.requestId,
    afterSeq: job.afterSeq,
    state: job.state,
    text: job.text,
    turnEnd: job.turnEnd,
    error: job.error,
    pendingInput: job.pendingInput,
    extensions: job.extensions ?? 0,
    lastObserved: job.lastObserved ?? null,
    lastReadError: job.lastReadError ?? null,
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
    expireAt: job.expireAt,
    suspended: job.suspended === true,
    settled: job.settled === true,
  }
}

/** Rehydrate the in-memory job fields from a persisted record. @param {any} record */
export function fromRecord(record) {
  return {
    id: record.jobId,
    sessionId: record.sessionId,
    requestId: record.requestId,
    afterSeq: record.afterSeq,
    started: true,
    settled: record.settled === true,
    checking: false,
    recheck: false,
    state: record.state,
    text: typeof record.text === 'string' ? record.text : '',
    turnEnd: record.turnEnd ?? null,
    error: record.error ?? null,
    pendingInput: Array.isArray(record.pendingInput) ? record.pendingInput : [],
    suspended: record.suspended === true,
    extensions: Number.isSafeInteger(record.extensions) ? record.extensions : 0,
    lastObserved: typeof record.lastObserved === 'string' ? record.lastObserved : null,
    lastReadError: record.lastReadError ?? null,
    createdAt: record.createdAt,
    updatedAt: record.updatedAt,
    expireAt: record.expireAt,
  }
}
