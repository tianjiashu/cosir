/**
 * 桌面端本地后端托管共享类型。
 *
 * 作为 Tauri IPC 命令的前后端共享契约，
 * 统一描述本地 Python 后端的生命周期状态、健康摘要与日志返回结构。
 *
 * @module shared/backend
 */

/** 桌面端感知到的本地后端生命周期状态。 */
export type DesktopBackendStatus =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "restarting"
  | "failed";

/** 后端 `/health` 结构化摘要。 */
export interface BackendHealthSnapshot {
  /** 健康端点返回的状态文本。 */
  status: string;
  /** 当前模型服务商。 */
  modelProvider: string;
  /** 当前模型 API base URL。 */
  modelBaseUrl: string;
  /** 当前模型名称。 */
  modelName: string;
  /** 当前 thinking 模式。 */
  modelThinkingMode: string;
  /** 当前是否已读取到 API Key。 */
  hasModelApiKey: boolean;
}

/** 本地后端错误摘要。 */
export interface BackendErrorSummary {
  /** 错误发生阶段，例如 `launch`、`health_check`。 */
  stage: string;
  /** 面向用户的摘要消息。 */
  message: string;
  /** 详细错误文本。 */
  detail: string;
  /** 错误发生时间（ISO-8601）。 */
  occurredAt: string;
}

/** 本地后端当前状态响应。 */
export interface BackendStatusResponse {
  /** 当前生命周期状态。 */
  status: DesktopBackendStatus;
  /** 当前后端是否由桌面端托管。 */
  managed: boolean;
  /** 当前已知的进程 ID。 */
  pid: number | null;
  /** 当前约定的监听端口。 */
  port: number;
  /** 最近一次成功启动时间。 */
  startedAt: string | null;
  /** 最近一次健康摘要。 */
  health: BackendHealthSnapshot | null;
  /** 最近一次结构化错误。 */
  lastError: BackendErrorSummary | null;
  /** 仓库根目录绝对路径。 */
  repoRoot: string;
  /** 后端工作目录绝对路径。 */
  backendDir: string;
  /** Python 可执行文件绝对路径。 */
  pythonBinary: string;
}

/** 单个日志文件的尾部内容。 */
export interface BackendLogTailEntry {
  /** 日志文件绝对路径。 */
  path: string;
  /** 尾部文本。 */
  content: string;
}

/** 日志尾部查询响应。 */
export interface BackendLogsTailResponse {
  /** 多个日志文件的尾部片段。 */
  entries: BackendLogTailEntry[];
}
