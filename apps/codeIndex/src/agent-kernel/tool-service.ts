/**
 * agent-kernel tool-service —— 查询分发适配层。
 *
 * 职责边界（判断二 + 3.2）：
 * - 把 `method` + `workspace_path` 映射为上游 `ToolHandler.execute(toolName, args)`；
 * - `workspace_path` 映射为 `args.projectPath`，由上游 `projectCache` 完成跨
 *   workspace 索引懒加载/路由（不在此重写核心逻辑）；
 * - 第一版直接复用 MCP 文本输出（ToolResult.content），不重新定义结构化响应。
 *
 * 不负责：进程生命周期、JSON-line 收发、引擎状态管理（归 server / workspace-service）。
 */

import type { ToolResult } from '../mcp/tools';
import type { MethodName } from './protocol';
import { KernelDispatchError, KernelErrorCode } from './protocol';
import { WorkspaceService } from './workspace-service';

/** 查询工具方法名（不含 kernel.* 握手方法）。 */
const QUERY_METHODS: ReadonlySet<MethodName> = new Set<MethodName>([
  'codegraph_explore',
  'codegraph_search',
  'codegraph_node',
  'codegraph_callers',
  'codegraph_callees',
  'codegraph_impact',
  'codegraph_files',
]);

/** 一次查询的归一化结果（第一版直接透传上游 ToolResult）。 */
export interface QueryOutcome {
  /** 上游返回的文本内容块。 */
  content: Array<{ type: string; text: string }>;
  /** 上游是否标记为错误（语义失败，非传输失败）。 */
  isError: boolean;
}

/**
 * 查询分发器。持有对 {@link WorkspaceService} 的引用以取得共享 ToolHandler。
 */
export class ToolService {
  constructor(private readonly workspaceService: WorkspaceService) {}

  /**
   * 判断方法是否为查询工具（而非 kernel.* 握手）。
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
  static isQueryMethod(method: MethodName): boolean {
    return QUERY_METHODS.has(method);
  }

  /**
   * 分发一次查询。
   *
   * 参数:
   *   method: 查询方法名（须为 QUERY_METHODS 之一）。
   *   params: 请求参数；必含 `workspace_path`，其余透传给上游 args。
   * 返回:
   *   QueryOutcome —— 归一化后的文本结果（含 isError 标记）。
   * 异常:
   *   抛出 KernelDispatchError（携带协议错误码），调用方（server.toKernelError）
   *   优先读取该 code 映射响应：
   *     - 方法非查询工具 / 缺 `workspace_path` → INVALID_REQUEST（不可重试）；
   *     - 引擎未初始化（ToolHandler 为 null，极端路径）→ KERNEL_UNAVAILABLE（可重试）。
   *   其余上游查询抛出的运行期错误由 server 归为 INTERNAL。
   * 副作用:
   *   首次遇到未初始化引擎时触发 workspaceService.ensureInitialized（幂等）。
   */
  async dispatch(method: MethodName, params: Record<string, unknown>): Promise<QueryOutcome> {
    if (!ToolService.isQueryMethod(method)) {
      throw new KernelDispatchError(
        KernelErrorCode.INVALID_REQUEST,
        `method ${method} is not a query tool`,
        false,
      );
    }

    const workspacePath = params['workspace_path'];
    if (typeof workspacePath !== 'string' || workspacePath.length === 0) {
      throw new KernelDispatchError(
        KernelErrorCode.INVALID_REQUEST,
        'workspace_path is required for query tools',
        false,
      );
    }

    // 引擎构造即同步创建 ToolHandler，故 getToolHandler() 永非 null；
    // 判定就绪须看 default project 是否真打开（getStatus().ready），
    // 而非 handler 是否存在。未就绪时每次都调幂等的 ensureInitialized，
    // 复用上游「失败不抛、下次重试」契约（其内部 initPromise 已 singleflight）。
    if (!this.workspaceService.getStatus().ready) {
      await this.workspaceService.ensureInitialized(workspacePath);
    }
    const handler = this.workspaceService.getToolHandler();
    if (!handler) {
      throw new KernelDispatchError(
        KernelErrorCode.KERNEL_UNAVAILABLE,
        'tool handler unavailable: engine not initialized',
        true,
      );
    }

    // 取出 workspace_path，避免把它当作上游不认识的参数透传。
    const args: Record<string, unknown> = { ...params };
    delete args['workspace_path'];
    args['projectPath'] = workspacePath;

    const result: ToolResult = await handler.execute(method, args);
    return {
      content: result.content.map((block) => ({ type: block.type, text: block.text })),
      isError: result.isError === true,
    };
  }
}
