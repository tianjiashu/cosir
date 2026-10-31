/**
 * 渲染进程与 Electron 宿主之间的唯一控制面契约：IPC 通道名与载荷类型。
 *
 * main（`electron/main/ipc.ts` 注册处理器并校验入参）、preload（`electron/preload/index.ts` 暴露
 * `window.cosirHost`）与渲染进程（只经 `lib/desktop-host/` 取用）三方共用这一份定义。新增宿主能力必须
 * 先判断它属于宿主控制面还是后端业务接口；属于宿主的，在这里加通道与类型，并同步 `ipc.ts` 的入参校验。
 *
 * 本文件只放类型与常量，不得引入 Node 或 Electron 运行时依赖：渲染进程也会编译它。
 */

export const HOST_CHANNEL = {
  backendSnapshot: "cosir:backend.snapshot",
  backendRestart: "cosir:backend.restart",
  /** main → 渲染进程的推送：后端状态、generation 或地址任一变化时发送完整快照。 */
  backendChanged: "cosir:backend.changed",
  logWrite: "cosir:log.write",
  dialogOpenDirectory: "cosir:dialog.open-directory",
  windowThemeSource: "cosir:window.theme-source",
} as const;

/**
 * main 处理器的统一应答。宿主失败以 `ok: false` 加中文 `message` 返回，preload 再转成 `Error`：
 * Electron 原生的 invoke 拒绝会把消息包成「Error invoking remote method …」，不能直接展示给用户。
 */
export type HostReply<T> = { ok: true; value: T } | { ok: false; message: string };

export type BackendStatus =
  | { state: "starting" | "ready" | "stopped" }
  | { state: "failed"; message: string };

/**
 * 宿主 supervisor 的完整状态快照。`backendBaseUrl` 只在 `ready` 时非空；`generation` 每次启动、重启或
 * 崩溃恢复都递增，渲染进程据此丢弃过期快照，不会被旧后端实例的状态覆盖。
 */
export type BackendSnapshot = {
  generation: number;
  status: BackendStatus;
  backendBaseUrl: string | null;
};

export const FRONTEND_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
export type FrontendLogLevel = (typeof FRONTEND_LOG_LEVELS)[number];

/** 前端日志行；字段与后端、桌面日志共用同一 JSONL 契约，main 在写入前逐项校验。 */
export type FrontendLogEntry = {
  ts: string;
  level: FrontendLogLevel;
  logger: string;
  trace_id: string;
  caller: string;
  event: string;
  msg: string;
  data: Record<string, unknown>;
  error: { type?: string; message: string; stack?: string; truncated: boolean } | null;
  truncated: boolean;
};

export type OpenDirectoryOptions = {
  title: string;
  defaultPath?: string;
};

export type HostPlatform = "darwin" | "win32" | "linux";

export const THEME_SOURCES = ["system", "light", "dark"] as const;
type ThemeSource = (typeof THEME_SOURCES)[number];

export type CosirHost = {
  platform: HostPlatform;
  backend: {
    snapshot(): Promise<BackendSnapshot>;
    /** 停止当前后端、重置崩溃恢复次数并重新启动，等到就绪或失败才返回。 */
    restart(): Promise<BackendSnapshot>;
    /** 订阅状态推送，返回取消订阅函数。 */
    onChange(listener: (snapshot: BackendSnapshot) => void): () => void;
  };
  log: {
    write(entry: FrontendLogEntry): Promise<void>;
  };
  dialog: {
    /** 原生目录选择框；用户取消时为 null。目录路径只来自这里，是目录授权的边界。 */
    openDirectory(options: OpenDirectoryOptions): Promise<string | null>;
  };
  window: {
    /**
     * 同步用户的外观偏好（不是解析后的明暗）：宿主据此设置原生外观，系统对话框、菜单与 Windows 窗口按钮
     * 随之切换。传偏好而不是结果，是因为固定成明或暗之后，页面的 `prefers-color-scheme` 就不再跟随系统。
     */
    setThemeSource(source: ThemeSource): Promise<void>;
  };
  files: {
    /**
     * 用户经选择框、粘贴或拖放交给页面的 `File` 的本机路径；非本机文件（如剪贴板截图）为空串。
     * 渲染进程只能对自己手里的 `File` 取路径，无法借它探测任意路径。
     */
    pathOf(file: File): string;
  };
};
