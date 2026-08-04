/**
 * agent-kernel 进程入口 —— stdio JSON-line RPC 服务。
 *
 * 职责边界（3.1）：
 * - 解析启动参数、实例化 {@link WorkspaceService} 与 {@link ToolService}；
 * - 监听 stdin 的 JSON-line 请求，写 stdout 的 JSON-line 响应；
 * - 实现 kernel.hello / kernel.ping / kernel.shutdown 握手 + 查询方法转发；
 * - stderr 只写内部日志，绝不写 RPC 响应。
 *
 * 不负责：进程发现 / 端口监听 / 索引业务语义 / 跨语言协议翻译（归后端 client）。
 */

import { createRequire } from 'module';
import {
  PROTOCOL_VERSION,
  KernelErrorCode,
  KernelDispatchError,
  parseLine,
  serialize,
  okResponse,
  errorResponse,
  type MethodName,
  type RpcRequest,
  type RpcResponse,
  type KernelErrorObject,
  type HelloResult,
  type PingResult,
  type QueryResultPayload,
} from './protocol';
import { WorkspaceService } from './workspace-service';
import { ToolService } from './tool-service';
import { LifecycleService } from './lifecycle-service';

// `require` 是 commonjs 模块顶部 TS 保留名，故用 createRequire 派生别名读取包版本。
const requireFrom = createRequire(__filename);
const pkg = requireFrom('../../package.json') as { version: string };

/** 查询方法名列表（不含 kernel.* 握手与生命周期），用于只读分发与 hello.capabilities。 */
const QUERY_METHOD_NAMES: MethodName[] = [
  'codegraph_explore',
  'codegraph_search',
  'codegraph_node',
  'codegraph_callers',
  'codegraph_callees',
  'codegraph_impact',
  'codegraph_files',
];

/** 生命周期方法名列表（写入型索引动作），用于 hello.capabilities。 */
const LIFECYCLE_METHOD_NAMES: MethodName[] = [
  'codegraph_status',
  'codegraph_init',
  'codegraph_sync',
];

/** hello.capabilities 全量 RPC 方法（不含 kernel.* 握手）。 */
const CAPABILITY_METHOD_NAMES: MethodName[] = [...QUERY_METHOD_NAMES, ...LIFECYCLE_METHOD_NAMES];

/**
 * stdio JSON-line RPC 服务。把进站请求按 method 路由到握手处理、lifecycle 或 tool-service。
 */
export class RpcServer {
  private readonly workspaceService = new WorkspaceService();
  private readonly toolService = new ToolService(this.workspaceService);
  private readonly lifecycleService = new LifecycleService();
  private stdinBuffer = '';
  private shuttingDown = false;

  /**
   * 启动服务：接管 stdin/stdout，开始消费 JSON-line 请求。
   *
   * 参数:
   *   无。
   * 返回:
   *   无。
   * 异常:
   *   无（启动失败仅写 stderr 日志）。
   * 副作用:
   *   注册 process.stdin 的 data/end 监听；进程内状态进入服务态。
   */
  start(): void {
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk: string) => this.onStdin(chunk));
    process.stdin.on('end', () => this.gracefulExit());
    process.stderr.write('[agent-kernel] RPC server started\n');
  }

  /**
   * stdin 分片累积并按行分发。JSON-line 可能因缓冲被截断，故按 \n 切分，
   * 末尾残留的半行留待下次 data。
   *
   * 参数:
   *   chunk: 本次读到的 stdin 文本块。
   * 返回:
   *   无。
   * 异常:
   *   无（单行解析失败只写 stderr，不中断流）。
   * 副作用:
   *   累积/消费 this.stdinBuffer；对每条完整行派发 handleRequest。
   */
  private onStdin(chunk: string): void {
    this.stdinBuffer += chunk;
    let newlineIndex = this.stdinBuffer.indexOf('\n');
    while (newlineIndex !== -1) {
      const line = this.stdinBuffer.slice(0, newlineIndex);
      this.stdinBuffer = this.stdinBuffer.slice(newlineIndex + 1);
      void this.handleLine(line);
      newlineIndex = this.stdinBuffer.indexOf('\n');
    }
  }

  /**
   * 处理单行：解析失败（非 RPC）写 stderr 忽略；解析成功则异步处理并回写响应。
   *
   * 参数:
   *   line: 单行文本。
   * 返回:
   *   无。
   * 异常:
   *   无。
   * 副作用:
   *   可能写 stdout 响应（经 handleRequest）。
   */
  private async handleLine(line: string): Promise<void> {
    const req = parseLine(line);
    if (!req) {
      process.stderr.write(`[agent-kernel] ignoring non-RPC stdin line\n`);
      return;
    }
    if (this.shuttingDown) {
      // 关闭中：除 kernel.shutdown 自身（幂等返回 ok）外，所有请求都返回
      // KERNEL_UNAVAILABLE 错误响应，避免调用方（如周期 ping）挂到超时。
      if (req.method === 'kernel.shutdown') {
        this.write(okResponse(req.id, { ok: true }));
        return;
      }
      this.write(
        errorResponse(req.id, {
          code: KernelErrorCode.KERNEL_UNAVAILABLE,
          message: 'kernel is shutting down',
          retryable: true,
        }),
      );
      return;
    }
    const res = await this.handleRequest(req);
    this.write(res);
  }

  /**
   * 按 method 路由请求。
   *
   * 参数:
   *   req: 已解析的 RpcRequest。
   * 返回:
   *   Promise<RpcResponse> —— 成功或错误响应。
   * 异常:
   *   不应抛出（所有异常在此收敛为错误响应）。
   * 副作用:
   *   kernel.shutdown 会触发 gracefulExit（异步退出进程）。
   */
  private async handleRequest(req: RpcRequest): Promise<RpcResponse> {
    try {
      switch (req.method) {
        case 'kernel.hello':
          return okResponse(req.id, this.hello());
        case 'kernel.ping':
          return okResponse(req.id, this.ping());
        case 'kernel.shutdown':
          // 响应由 handleLine 统一刷出；setImmediate 把优雅退出排到当前
          // 微任务之后，确保响应先于进程退出送达。
          setImmediate(() => this.gracefulExit());
          return okResponse(req.id, { ok: true });
        default:
          // 生命周期方法（写入型）须先于只读查询分支判定——两者方法名前缀同为
          // `codegraph_`，若先落入查询分发会被 isQueryMethod 判为未知而 INVALID_REQUEST。
          if (LifecycleService.isLifecycleMethod(req.method as string)) {
            const params = req.params ?? {};
            return okResponse(req.id, await this.lifecycleService.handle(req.method, params));
          }
          // 未知方法名（非查询工具）属请求形状非法，不应落入查询分发。
          if (!ToolService.isQueryMethod(req.method as MethodName)) {
            return errorResponse(req.id, {
              code: KernelErrorCode.INVALID_REQUEST,
              message: `unknown method: ${req.method}`,
              retryable: false,
            });
          }
          return await this.handleQuery(req);
      }
    } catch (err) {
      const error = this.toKernelError(err, req.method);
      return errorResponse(req.id, error);
    }
  }

  /**
   * 查询方法处理：委托 tool-service 分发，并把 QueryOutcome 包成结果。
   *
   * 参数:
   *   req: 含查询 method 与 params 的请求。
   * 返回:
   *   Promise<RpcResponse>。
   * 异常:
   *   上抛 dispatch 抛出的确定性错误（由 handleRequest 收敛为错误响应）。
   * 副作用:
   *   可能触发引擎懒初始化（首次查询）。
   */
  private async handleQuery(req: RpcRequest): Promise<RpcResponse> {
    const params = req.params ?? {};
    const outcome = await this.toolService.dispatch(req.method, params);
    const result: QueryResultPayload = {
      content: outcome.content,
      is_error: outcome.isError,
    };
    return okResponse(req.id, result);
  }

  /**
   * 构造 kernel.hello 响应。
   *
   * 参数:
   *   无。
   * 返回:
   *   HelloResult。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  private hello(): HelloResult {
    return {
      protocol_version: PROTOCOL_VERSION,
      kernel_version: pkg.version,
      codegraph_version: pkg.version,
      capabilities: CAPABILITY_METHOD_NAMES,
      platform: process.platform,
    };
  }

  /**
   * 构造 kernel.ping 响应。
   *
   * 参数:
   *   无。
   * 返回:
   *   PingResult。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  private ping(): PingResult {
    return {
      ok: true,
      uptime_ms: this.workspaceService.uptimeMs(),
      active_workspaces: this.workspaceService.getStatus().ready ? 1 : 0,
    };
  }

  /**
   * 把异常收敛为协议错误对象。
   *
   * 参数:
   *   err: 捕获的任意错误。
   *   method: 出错的方法名（用于日志与未索引判定）。
   * 返回:
   *   KernelErrorObject。
   * 异常:
   *   无。
   * 副作用:
   *   无。
   */
  private toKernelError(err: unknown, method: MethodName): KernelErrorObject {
    const message = err instanceof Error ? err.message : String(err);
    process.stderr.write(`[agent-kernel] ${method} failed: ${message}\n`);
    // 适配层抛出的结构化错误已携带协议错误码，优先采用，避免 message 子串猜测。
    if (err instanceof KernelDispatchError) {
      return { code: err.code, message, retryable: err.retryable };
    }
    // 其余异常：因 tool-service 已用 KernelDispatchError 收敛所有确定性失败，
    // 落到此处的多为上游查询抛出的未索引/不可达等运行期错误 → 归 INTERNAL。
    return { code: KernelErrorCode.INTERNAL, message, retryable: false };
  }

  /**
   * 写出站响应（单行 JSON）。
   *
   * 参数:
   *   res: RpcResponse。
   * 返回:
   *   无。
   * 异常:
   *   无。
   * 副作用:
   *   写 process.stdout。
   */
  private write(res: RpcResponse): void {
    process.stdout.write(serialize(res) + '\n');
  }

  /**
   * 优雅退出：关闭引擎并终止进程。
   *
   * 参数:
   *   无。
   * 返回:
   *   无。
   * 异常:
   *   无。
   * 副作用:
   *   置 shuttingDown；关闭 WorkspaceService；process.exit(0)。
   */
  private gracefulExit(): void {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    this.workspaceService.stop();
    process.exit(0);
  }
}

/** 进程入口：实例化并启动 RPC 服务。 */
function main(): void {
  const server = new RpcServer();
  server.start();
}

main();
