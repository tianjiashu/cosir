/**
 * agent-kernel 协议层 —— 纯类型与 JSON-line 序列化辅助，无副作用。
 *
 * 该文件只定义子进程 stdio 上收发的 RPC 消息形状、握手类型与错误码，
 * 不依赖 `mcp/`、`index.ts` 等任何上游运行时模块，便于 `server.ts` /
 * `workspace-service.ts` / `tool-service.ts` 共用且独立单测。
 *
 * 不负责：进程生命周期、索引业务、查询语义 —— 这些归 `server.ts` 与各 service。
 */

/** 当前 agent-kernel 协议版本。Supervisor 握手时比对，不兼容即拒绝。 */
export const PROTOCOL_VERSION = '1.1.0';

/** Kernel 错误码 —— 与后端 `CodeGraphKernelError` 一一对应。 */
export enum KernelErrorCode {
  /** 协议版本不兼容（握手阶段）。 */
  PROTOCOL_INCOMPATIBLE = 'PROTOCOL_INCOMPATIBLE',
  /** 请求超时。 */
  TIMEOUT = 'TIMEOUT',
  /** Kernel 不可用（未就绪 / 已退出 / 内部致命）。 */
  KERNEL_UNAVAILABLE = 'KERNEL_UNAVAILABLE',
  /** workspace 未建索引（无 .codegraph/ 或未初始化成功）。 */
  WORKSPACE_NOT_INDEXED = 'WORKSPACE_NOT_INDEXED',
  /** 工具被 CODEGRAPH_MCP_TOOLS 裁剪拒绝。 */
  TOOL_NOT_ALLOWED = 'TOOL_NOT_ALLOWED',
  /** 请求形状非法（缺 id / 缺 method / params 类型错）。 */
  INVALID_REQUEST = 'INVALID_REQUEST',
  /** 索引写入（init/sync）执行失败。 */
  INDEXING_FAILED = 'INDEXING_FAILED',
  /** 索引被占用（另一进程/实例持锁，含 SQLite 忙碌）。 */
  INDEX_LOCKED = 'INDEX_LOCKED',
  /** 其他内部错误。 */
  INTERNAL = 'INTERNAL',
}

/** 结构化错误对象 —— 经 JSON-line 回传 Supervisor，再映射为 Python 异常。 */
export interface KernelErrorObject {
  code: KernelErrorCode;
  message: string;
  /** 是否值得重试（瞬态 true / 确定性 false）。 */
  retryable: boolean;
}

/**
 * 适配层抛出的结构化错误：携带协议错误码，使 server 能精确映射响应码，
 * 而非依赖 message 子串猜测。语义确定（缺参=INVALID_REQUEST 等）、可重试标记随码。
 */
export class KernelDispatchError extends Error {
  constructor(
    public readonly code: KernelErrorCode,
    message: string,
    public readonly retryable: boolean = false,
  ) {
    super(message);
    this.name = 'KernelDispatchError';
  }
}

/** 请求方法名：握手 + 全部查询工具 + 生命周期方法（工具名以上游 dispatchTool 路由为准）。 */
export type MethodName =
  | 'kernel.hello'
  | 'kernel.ping'
  | 'kernel.shutdown'
  | 'codegraph_explore'
  | 'codegraph_search'
  | 'codegraph_node'
  | 'codegraph_callers'
  | 'codegraph_callees'
  | 'codegraph_impact'
  | 'codegraph_files'
  | 'codegraph_status'
  | 'codegraph_init'
  | 'codegraph_sync';

/** 入站请求：stdin 收到的每一行解析为此形状。 */
export interface RpcRequest {
  /** 调用方生成的唯一请求 id；响应须原样带回。 */
  id: string;
  method: MethodName;
  /** 方法参数；查询方法必带 `workspace_path`。 */
  params?: Record<string, unknown>;
}

/** 出站响应：stdout 写出的每一行。 */
export interface RpcResponse {
  id: string;
  result?: unknown;
  error?: KernelErrorObject;
}

/**
 * 出站通知（预留）：无需回应的单向消息（如启动进度）。
 * 第一阶段未使用，但协议预留以免后续破坏 JSON-line 形状。
 */
export interface RpcNotification {
  method: string;
  params?: Record<string, unknown>;
}

/** 查询方法响应负载（第一版直接透传上游 ToolResult 文本）。 */
export interface QueryResultPayload {
  /** 上游返回的文本内容块。 */
  content: Array<{ type: string; text: string }>;
  /** 上游是否标记为错误（语义失败，非传输失败），蛇形以对齐协议其余字段。 */
  is_error: boolean;
}

/** `kernel.hello` 响应：Kernel 自报家门。 */
export interface HelloResult {
  protocol_version: string;
  /** 本 agent-kernel 构建版本（取自 package.json version）。 */
  kernel_version: string;
  /** 内嵌 CodeGraph 版本（取自 package.json version）。 */
  codegraph_version: string;
  /** 本 Kernel 暴露的查询方法名列表（不含 kernel.* 握手方法）。 */
  capabilities: MethodName[];
  platform: NodeJS.Platform;
}

/** `kernel.ping` 响应。 */
export interface PingResult {
  ok: true;
  uptime_ms: number;
  /** 已成功初始化（default project）的 workspace 数（0 或 1，第一阶段最多 1）。 */
  active_workspaces: number;
}

/** 归一化索引状态（status 与 action 共用，避免两侧取值域漂移）。 */
export type IndexState = 'unindexed' | 'indexing' | 'ready' | 'failed';

/** `codegraph_status` 响应。 */
export interface IndexStatusPayload {
  state: IndexState;
  /** 最近一次索引完成时间戳（ms）；未索引为 null。 */
  last_indexed_at: number | null;
}

/** `codegraph_init` 响应。字段直取上游 IndexResult。 */
export interface IndexInitPayload {
  state: IndexState; // 正常为 'ready'
  files_indexed: number; // IndexResult.filesIndexed
  duration_ms: number; // IndexResult.durationMs（上游自带，不重复计时）
}

/** `codegraph_sync` 响应。SyncResult 与 IndexResult 字段异构，故独立类型。 */
export interface IndexSyncPayload {
  state: IndexState; // 正常为 'ready'
  files_added: number; // SyncResult.filesAdded
  files_modified: number; // SyncResult.filesModified
  files_removed: number; // SyncResult.filesRemoved
  duration_ms: number; // SyncResult.durationMs
}

/**
 * 类型守卫：判断任意解析结果是否为合法 {@link RpcRequest}。
 * 仅校验「有 string 类型 id 与 method」，params 可为空对象/缺失。
 */
export function isRpcRequest(obj: unknown): obj is RpcRequest {
  if (typeof obj !== 'object' || obj === null) return false;
  const candidate = obj as Record<string, unknown>;
  return typeof candidate.id === 'string' && typeof candidate.method === 'string';
}

/**
 * 把单行文本解析为 {@link RpcRequest}；非 JSON / 非对象 / 形状非法返回 null。
 * 调用方应忽略 null（视作内部日志行，不视为 RPC）。
 */
export function parseLine(line: string): RpcRequest | null {
  const trimmed = line.trim();
  if (trimmed.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  return isRpcRequest(parsed) ? parsed : null;
}

/**
 * 把任意出站消息序列化为单行 JSON（不含换行）。
 * 用于写入 stdout / stderr 桥接；JSON-line 协议要求每行一条消息。
 */
export function serialize(obj: RpcResponse | RpcNotification): string {
  return JSON.stringify(obj);
}

/** 构造一个错误响应。 */
export function errorResponse(id: string, error: KernelErrorObject): RpcResponse {
  return { id, error };
}

/** 构造一个成功响应。 */
export function okResponse(id: string, result: unknown): RpcResponse {
  return { id, result };
}
