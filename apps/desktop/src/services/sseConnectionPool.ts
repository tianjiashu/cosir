/**
 * SSE 连接池（进程级单例）。
 *
 * 管理全部活跃 SSE 连接的生命周期，按 turnId 索引，并额外记录每个连接所属的 task
 * 以便按任务维度批量断开。
 *
 * **为什么必须是模块级单例，而不是 ``useSSE`` 内的 ``useRef``**：
 * ``useTask()`` 在多个组件中各自实例化（InputBar / Sidebar / NewTaskPage /
 * useStartupTaskResume），每个实例都会独立调用 ``useSSE()``。若连接池是 hook 内的
 * useRef，则每个实例持有彼此隔离的空池——InputBar 建立的流，Sidebar 永远看不到，
 * 删除任务时的断流会静默退化成空操作，残留流继续投递事件。连接池是「一个进程一份」
 * 的运行期资源，其唯一正确的宿主是模块作用域。
 *
 * 本模块不负责：事件的攒批与分发（``useSSE``）、连接的建立与协议解析（``SSEConnection``）、
 * 事件状态的落库（各 store）。
 *
 * @module services/sseConnectionPool
 */

import type { SSEConnection } from "./sse";
import { logError } from "@/lib/logger";

/** 连接池条目：SSE 连接、其所属 task，以及其事件缓冲的 flush 回调。 */
export interface PooledConnection {
  /** SSE 连接实例。 */
  connection: SSEConnection;
  /** 该连接所属任务（后端 int 主键），供按 task 维度批量断开。 */
  taskId: number;
  /**
   * 冲刷该连接所属事件缓冲的回调。
   *
   * 连接的攒批缓冲由建立它的 ``useSSE`` 实例持有（``useRef``），而连接池是全进程
   * 共享的。断开方常常不是建立方（如 Sidebar 删除 InputBar 建立的流），若断开时只
   * 回调调用方自己的 flush，就会冲刷到无关的缓冲并漏掉目标连接的残留事件。因此
   * flush 必须随连接一起登记，由池在断开前按条目精确回调。
   */
  flush: () => void;
}

/**
 * 按 turnId 索引的 SSE 连接池。
 *
 * 键为 ``String(turnId)``（含 `"temp-<uuid>"` 形态的乐观轮次），与 SSE URL 层一致；
 * 任务主键在本模块内保持 number，与 store 的 idUnify 约定对齐。
 */
class SSEConnectionPool {
  /** 活跃连接，键为 String(turnId)。 */
  private readonly connections = new Map<string, PooledConnection>();

  /**
   * 取出指定 turn 的连接条目。
   *
   * @param turnKey - 轮次键（``String(turnId)``）。
   * @returns 对应条目；无活跃连接时返回 undefined。
   */
  get(turnKey: string): PooledConnection | undefined {
    return this.connections.get(turnKey);
  }

  /**
   * 放入连接，并断开同键上已存在的旧连接。
   *
   * @param turnKey - 轮次键（``String(turnId)``）。
   * @param entry - 待放入的连接条目。
   * @returns 被替换掉的旧条目（已断开）；原先无连接时返回 undefined。
   */
  replace(turnKey: string, entry: PooledConnection): PooledConnection | undefined {
    const previous = this.connections.get(turnKey);
    if (previous) {
      previous.connection.disconnect();
    }
    this.connections.set(turnKey, entry);
    return previous;
  }

  /**
   * 断开并从池中移除指定 turn 的连接。
   *
   * 断开前先冲刷该连接自己的事件缓冲，确保 disconnect 之前已到达的事件不会滞留。
   *
   * @param turnKey - 轮次键（``String(turnId)``）。
   * @returns 被断开的条目；该 turn 无连接时返回 undefined。
   */
  remove(turnKey: string): PooledConnection | undefined {
    const pooled = this.connections.get(turnKey);
    if (!pooled) {
      return undefined;
    }
    this.connections.delete(turnKey);
    this.disconnectSafely(pooled);
    return pooled;
  }

  /**
   * 断开指定 task 下全部 turn 的连接（不影响其它 task）。
   *
   * 删除任务时调用：后端已级联删除该任务的轮次与事件，其残留流若不断开，会继续
   * 投递事件并在 eventStore / turnStore / contextUsageStore 中重建已删任务的条目。
   * 连接被 abort 后不再产出事件，因此断流先于本地缓存清理执行即可保证无残留复活。
   *
   * @param taskId - 待断开连接的任务标识（number，后端 int 主键）。
   * @returns 本次实际断开的轮次键列表（该 task 无活跃连接时为空数组）。
   */
  removeByTask(taskId: number): string[] {
    return this.removeWhere((pooled) => pooled.taskId === taskId);
  }

  /**
   * 断开指定多个 task 下全部 turn 的连接（不影响其它 task）。
   *
   * 删除工作区时调用：后端级联删除该工作区下的全部任务，其残留流若不断开，会继续
   * 投递事件并在各 store 中重建已删任务的条目——与单任务删除同理，只是作用域扩大到
   * 一组任务。
   *
   * @param taskIds - 待断开连接的任务标识列表（number，后端 int 主键）。
   * @returns 本次实际断开的轮次键列表（这些 task 无活跃连接时为空数组）。
   */
  removeByTasks(taskIds: number[]): string[] {
    const targets = new Set(taskIds);
    return this.removeWhere((pooled) => targets.has(pooled.taskId));
  }

  /**
   * 断开全部连接并清空池（整体重置，不冲刷缓冲）。
   *
   * **严禁用于任何生产清理路径**——仅限测试隔离与整体复位。
   *
   * 与 ``remove*`` 系列的关键差别：**不调用** ``flush``。本方法是「丢弃全部运行期
   * 状态」的语义（测试重置、整体复位），冲刷残留事件反而会把本该丢弃的数据写进
   * 各 store；而 ``remove`` / ``removeByTask`` / ``removeByTasks`` 是「正常收尾一条
   * 流」，必须先把已到达的事件冲刷完再断开。生产若误用本方法清理某组连接，会静默
   * 丢失那些连接缓冲中尚未落库的事件。
   *
   * 本方法同时是单例的测试重置缝：连接池是模块级单例，用例之间会互相串味，
   * beforeEach 必须有一个「清空全部」的出口才能隔离。
   */
  removeAll(): void {
    this.connections.forEach((pooled) => pooled.connection.disconnect());
    this.connections.clear();
  }

  /**
   * 按谓词断开并从池中移除连接。
   *
   * 统一「先出池 → 再冲刷该连接自己的缓冲 → 最后断开」的顺序：先出池可避免 flush
   * 触发的同步副作用再次命中该条目；先 flush 可保证断开前已到达的事件不滞留。
   *
   * @param predicate - 返回 true 表示该连接应被断开。
   * @returns 本次实际断开的轮次键列表。
   */
  private removeWhere(predicate: (pooled: PooledConnection) => boolean): string[] {
    const removed: string[] = [];
    const matched: Array<[string, PooledConnection]> = [];
    for (const entry of this.connections) {
      if (predicate(entry[1])) {
        matched.push(entry);
      }
    }
    for (const [turnKey, pooled] of matched) {
      this.connections.delete(turnKey);
      this.disconnectSafely(pooled);
      removed.push(turnKey);
    }
    return removed;
  }

  /**
   * 冲刷条目缓冲后断开连接，且**保证断开一定执行**。
   *
   * 条目在调用前已从池中移除，若 flush 抛异常而 disconnect 被跳过，该连接就变成
   * 「已出池但仍活着」的泄漏连接——既无法再被任何池操作触及，又会继续投递事件在
   * 各 store 中重建已删任务的条目。故 disconnect 必须放在 finally 中。
   *
   * flush 的异常本身只记录不向上抛：批量断开时单条失败不应中断其余连接，也不应
   * 让调用方（删除任务 / 删除工作区流程）因一个缓冲冲刷失败而整体失败。
   *
   * @param pooled - 已从池中移除、待断开的条目。
   */
  private disconnectSafely(pooled: PooledConnection): void {
    try {
      pooled.flush();
    } catch (error) {
      logError("冲刷 SSE 事件缓冲失败，连接仍将被断开", error, { module: "sseConnectionPool" });
    } finally {
      pooled.connection.disconnect();
    }
  }
}

/** SSE 连接池进程级单例：连接是运行期全局资源，全进程唯一。 */
export const sseConnectionPool = new SSEConnectionPool();
