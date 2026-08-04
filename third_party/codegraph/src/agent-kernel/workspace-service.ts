/**
 * agent-kernel workspace-service —— 持有单一共享 {@link MCPEngine} 的常驻适配层。
 *
 * 职责边界（判断二）：
 * - 只负责「应用级常驻一个 engine」与它的初始化/状态/停止；
 * - 不维护 workspace_path → engine 映射（跨 workspace 索引路由由上游
 *   `ToolHandler.projectCache` 经 `args.projectPath` 完成）；
 * - 不直接理解查询语义（那归 `tool-service`）。
 *
 * 不负责：进程生命周期、JSON-line 收发、业务结果解析。
 */

import { MCPEngine } from '../mcp/engine';
import type { ToolHandler } from '../mcp/tools';

/** workspace-service 对外暴露的引擎状态。 */
export interface EngineStatus {
  /** default project 的 CodeGraph 是否已打开（即引擎是否真正就绪可查）。 */
  ready: boolean;
  /** 引擎解析到的 default project 根（未初始化为 null）。 */
  projectPath: string | null;
  /** 引擎是否已关闭（关闭后不接受新查询）。 */
  closed: boolean;
}

/**
 * 单 engine 常驻管理器。direct mode：一个 stdio 会话、一个 engine、一个
 * 事件循环；不启 worker pool（第一阶段并发由 Kernel 内部串行/队列限流）。
 */
export class WorkspaceService {
  private engine: MCPEngine | null = null;
  private readonly startTimeMs = Date.now();

  /**
   * 懒创建并初始化共享 engine。
   *
   * 参数:
   *   workspacePath: 用作首次初始化的 searchFrom；引擎会向上查找最近
   *     `.codegraph/` 作为 default project（承载 watcher）。
   * 返回:
   *   无。
   * 异常:
   *   不抛；初始化失败仅记录到 default project 为 null，调用方经 getStatus 感知。
   * 副作用:
   *   首次调用创建 MCPEngine 并触发（singleflight）索引打开 + watcher 启动。
   */
  async ensureInitialized(workspacePath: string): Promise<void> {
    if (!this.engine) {
      this.engine = new MCPEngine({ watch: true, queryPool: false });
    }
    await this.engine.ensureInitialized(workspacePath);
  }

  /**
   * 当前引擎状态快照。
   *
   * 参数:
   *   无。
   * 返回:
   *   EngineStatus —— ready/projectPath/closed。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  getStatus(): EngineStatus {
    if (!this.engine) {
      return { ready: false, projectPath: null, closed: false };
    }
    return {
      ready: this.engine.hasDefaultCodeGraph(),
      projectPath: this.engine.getProjectPath(),
      closed: false,
    };
  }

  /**
   * 暴露共享 ToolHandler 供 tool-service 分发查询。
   *
   * 参数:
   *   无。
   * 返回:
   *   ToolHandler —— 引擎未创建时返回 null（调用方应先行 ensureInitialized）。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  getToolHandler(): ToolHandler | null {
    return this.engine ? this.engine.getToolHandler() : null;
  }

  /**
   * 进程就绪时长（毫秒），供 kernel.ping 上报。
   *
   * 参数:
   *   无。
   * 返回:
   *   number —— 自 WorkspaceService 实例化到现在的毫秒数。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  uptimeMs(): number {
    return Date.now() - this.startTimeMs;
  }

  /**
   * 优雅关闭引擎，释放 watcher / query pool / SQLite 连接。
   *
   * 参数:
   *   无。
   * 返回:
   *   无。
   * 异常:
   *   无（引擎 stop 内部吞掉关闭异常）。
   * 副作用:
   *   置 engine 为 null；后续 getToolHandler 返回 null。
   */
  stop(): void {
    if (this.engine) {
      this.engine.stop();
      this.engine = null;
    }
  }
}
