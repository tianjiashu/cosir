/**
 * agent-kernel lifecycle-service —— 写入型 Workspace 索引生命周期适配层。
 *
 * 职责边界（方案一 §5.2）：
 * - 提供 `status` / `init` / `sync` 三个写入型生命周期动作；
 * - root 解析用上游 `findNearestCodeGraphRoot`（与只读通道 MCPEngine / ToolHandler
 *   语义一致，避免 monorepo 子目录建重复索引）；
 * - `init` 按 `isInitialized` 分流：未初始化走 `CodeGraph.init` + `indexAll`，
 *   已初始化但无索引记录走 `open` + `indexAll`（吸收上游 init 对已初始化目录抛异常）；
 * - 自持 `instances` 缓存，不复用只读 `ToolHandler.projectCache`（写入与只读解耦）。
 *
 * 不负责：进程生命周期、JSON-line 收发（归 server）、只读查询（归 tool-service）、
 * 删除与重建（第二阶段不提供）。
 */

import type { IndexInitPayload, IndexStatusPayload, IndexSyncPayload, IndexState } from './protocol';
import { CodeGraph } from '../index';
import { findNearestCodeGraphRoot, isInitialized } from '../directory';
import { KernelDispatchError, KernelErrorCode } from './protocol';

/** 判断是否为索引生命周期方法（区别于 tool-service 只读查询）。 */
const LIFECYCLE_METHODS = new Set<string>([
  'codegraph_status',
  'codegraph_init',
  'codegraph_sync',
]);

/**
 * 写入型索引生命周期服务。status / init / sync 独立于只读查询通道。
 */
export class LifecycleService {
  /** 已打开的 CodeGraph 实例缓存，键为规范化 workspace_path。 */
  private readonly instances = new Map<string, CodeGraph>();

  /**
   * 判断方法是否为生命周期方法（写入型）。
   *
   * 参数:
   *   method: 请求方法名。
   * 返回:
   *   boolean。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  static isLifecycleMethod(method: string): boolean {
    return LIFECYCLE_METHODS.has(method);
  }

  /**
   * 查询 workspace 索引状态。
   *
   * 参数:
   *   workspacePath: 待查 workspace 路径。
   * 返回:
   *   IndexStatusPayload —— 归一化状态与最近索引时间戳。
   * 异常:
   *   无（索引打开失败归为 failed 状态返回，不抛）。
   * 副作用:
   *   首次查询会 open 已存在索引的 CodeGraph 实例并缓存。
   */
  async status(workspacePath: string): Promise<IndexStatusPayload> {
    const resolvedRoot = findNearestCodeGraphRoot(workspacePath);
    if (resolvedRoot === null) {
      return { state: 'unindexed', last_indexed_at: null };
    }
    try {
      const cg = await this.openOrCached(resolvedRoot);
      const raw = cg.getIndexState();
      const lastIndexedAt = cg.getLastIndexedAt();
      const state = normalizeState(raw);
      return { state, last_indexed_at: lastIndexedAt };
    } catch {
      return { state: 'failed', last_indexed_at: null };
    }
  }

  /**
   * 创建索引（首次）或在已初始化目录上执行全量索引。
   *
   * 参数:
   *   workspacePath: 待建索引的 workspace 路径。
   * 返回:
   *   IndexInitPayload —— 索引摘要（files_indexed / duration_ms）。
   * 异常:
   *   抛出 KernelDispatchError：
   *     - INDEXING_FAILED（retryable）：索引构建抛错或未完全成功。
   *     - INDEX_LOCKED（retryable）：SQLite 忙碌 / 实例持锁。
   *   workspace 为空则 INVALID_REQUEST（不可重试）。
   * 副作用:
   *   在 workspace 下创建 .workspace_payload/ 并全量索引；实例入缓存。
   */
  async init(workspacePath: string): Promise<IndexInitPayload> {
    const resolvedRoot = findNearestCodeGraphRoot(workspacePath) ?? workspacePath;
    let cg: CodeGraph;
    try {
      if (!isInitialized(resolvedRoot)) {
        // 吸收上游「init 对已初始化目录抛异常」：未初始化才走 init（创建库），
        // 随后统一 indexAll 拿到 IndexResult（CodeGraph.init 不返回结果摘要）。
        cg = await CodeGraph.init(resolvedRoot, { index: false });
      } else {
        cg = await this.openOrCached(resolvedRoot);
      }
      const result = await this.withBusyGuard(() => cg.indexAll());
      if (!result.success) {
        throw new KernelDispatchError(
          KernelErrorCode.INDEXING_FAILED,
          `codegraph indexAll did not complete successfully`,
          true,
        );
      }
      this.instances.set(resolvedRoot, cg);
      return {
        state: 'ready',
        files_indexed: result.filesIndexed,
        duration_ms: result.durationMs,
      };
    } catch (err) {
      throw this.toLifecycleError(err);
    }
  }

  /**
   * 增量同步（D2：已索引则每次 task 前显式 sync）。
   *
   * 参数:
   *   workspacePath: 待同步的 workspace 路径。
   * 返回:
   *   IndexSyncPayload —— 增量摘要（added/modified/removed / duration_ms）。
   * 异常:
   *   抛出 KernelDispatchError：INDEXING_FAILED / INDEX_LOCKED（均可重试）；
   *   未索引（无 root）则 WORKSPACE_NOT_INDEXED（不可重试）。
   * 副作用:
   *   增量更新索引；实例入缓存。
   */
  async sync(workspacePath: string): Promise<IndexSyncPayload> {
    const resolvedRoot = findNearestCodeGraphRoot(workspacePath);
    if (resolvedRoot === null) {
      throw new KernelDispatchError(
        KernelErrorCode.WORKSPACE_NOT_INDEXED,
        `workspace has no codegraph index to sync: ${workspacePath}`,
        false,
      );
    }
    try {
      const cg = await this.openOrCached(resolvedRoot);
      const result = await this.withBusyGuard(() => cg.sync());
      return {
        state: 'ready',
        files_added: result.filesAdded,
        files_modified: result.filesModified,
        files_removed: result.filesRemoved,
        duration_ms: result.durationMs,
      };
    } catch (err) {
      throw this.toLifecycleError(err);
    }
  }

  /**
   * 分发一次生命周期方法调用（server 路由入口）。
   *
   * 参数:
   *   method: 生命周期方法名。
   *   params: 请求参数；必含 `workspace_path`。
   * 返回:
   *   IndexStatusPayload | IndexInitPayload | IndexSyncPayload。
   * 异常:
   *   KernelDispatchError：未知方法 / 缺 workspace_path → INVALID_REQUEST。
   * 副作用:
   *   委托到对应方法。
   */
  async handle(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (!LifecycleService.isLifecycleMethod(method)) {
      throw new KernelDispatchError(
        KernelErrorCode.INVALID_REQUEST,
        `method ${method} is not a lifecycle method`,
        false,
      );
    }
    const workspacePath = params['workspace_path'];
    if (typeof workspacePath !== 'string' || workspacePath.length === 0) {
      throw new KernelDispatchError(
        KernelErrorCode.INVALID_REQUEST,
        'workspace_path is required for lifecycle methods',
        false,
      );
    }
    switch (method) {
      case 'codegraph_status':
        return this.status(workspacePath);
      case 'codegraph_init':
        return this.init(workspacePath);
      case 'codegraph_sync':
        return this.sync(workspacePath);
      default:
        throw new KernelDispatchError(
          KernelErrorCode.INVALID_REQUEST,
          `unknown lifecycle method: ${method}`,
          false,
        );
    }
  }

  /** 取缓存实例，未命中则 open 并缓存。 */
  private async openOrCached(resolvedRoot: string): Promise<CodeGraph> {
    const cached = this.instances.get(resolvedRoot);
    if (cached) {
      return cached;
    }
    const cg = await CodeGraph.open(resolvedRoot);
    this.instances.set(resolvedRoot, cg);
    return cg;
  }

  /** 把 SQLite 忙碌 / 锁错误归一化为 INDEX_LOCKED，其余转 INDEXING_FAILED。 */
  private toLifecycleError(err: unknown): KernelDispatchError {
    if (err instanceof KernelDispatchError) {
      return err;
    }
    const message = err instanceof Error ? err.message : String(err);
    if (/database is locked|SQLITE_BUSY|SQLITE_LOCKED/.test(message)) {
      return new KernelDispatchError(KernelErrorCode.INDEX_LOCKED, message, true);
    }
    return new KernelDispatchError(KernelErrorCode.INDEXING_FAILED, message, true);
  }

  /** 包装写入动作，捕获上游抛出的 SQLite 忙碌等错误并归一化。 */
  private async withBusyGuard<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (err) {
      throw this.toLifecycleError(err);
    }
  }
}

/** 把上游 getIndexState 的原始取值归一化为协议 IndexState。 */
function normalizeState(raw: 'indexing' | 'complete' | 'partial' | 'failed' | null): IndexState {
  if (raw === null) return 'unindexed';
  if (raw === 'indexing') return 'indexing';
  if (raw === 'failed') return 'failed';
  // complete / partial 均视为可用（partial 由后端 sync 补齐，不阻断）。
  return 'ready';
}
