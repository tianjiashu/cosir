/** 宿主标签 id 为 t 加单调序号，IPC 与工具入参共用该格式。 */
export const BROWSER_TAB_ID = /^t\d+$/;

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
  browserState: "cosir:browser.state",
  browserOpen: "cosir:browser.open",
  /** main → 渲染进程的推送：标签（增删、地址、标题、加载、崩溃、弹窗）或下载任一变化时发送完整状态。 */
  browserStateChanged: "cosir:browser.state-changed",
  browserPlace: "cosir:browser.place",
  browserFreeze: "cosir:browser.freeze",
  browserNavigate: "cosir:browser.navigate",
  browserClose: "cosir:browser.close",
  browserAnswerDialog: "cosir:browser.answer-dialog",
  browserRevealDownload: "cosir:browser.reveal-download",
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

/** 谁开的标签：用户与 Agent 都能在 Task 里新开（与交互终端一致）。 */
export type BrowserTabCreator = "agent" | "user";

export const BROWSER_DIALOG_TYPES = ["alert", "confirm", "prompt"] as const;

/**
 * 页面弹出、尚未作答的 JS 弹窗。页面在作答前一直停着；用户（面板提示条）与 Agent（`browser_act` 的 `dialog`
 * 动作）都能作答，作答按 `id` 指明是哪一个，不是当前那个就不生效（防止过时的点击答给紧接着弹出的新弹窗）。
 */
export type BrowserDialog = {
  /** 宿主内单调递增。 */
  id: number;
  type: (typeof BROWSER_DIALOG_TYPES)[number];
  message: string;
  /** `prompt` 的默认值；其余类型为空串。 */
  defaultText: string;
};

/** 弹窗的答复：`text` 只对 `prompt` 有意义（接受时作为返回值，缺省用默认值）。 */
export type BrowserDialogAnswer = { accept: boolean; text?: string };

/**
 * Task 的一次下载，存进 Task 所在工作区的下载目录。下载归 Task 而不归标签（与浏览器的下载归 profile 一致）：
 * 触发它的标签关掉（常见于弹出页下载后自己关闭）不会取消下载。
 */
export type BrowserDownload = {
  id: string;
  taskId: number;
  /** 触发下载的标签（之后可能已关闭）。 */
  tabId: string;
  filename: string;
  /** 落盘的本机绝对路径。 */
  path: string;
  state: "progressing" | "completed" | "cancelled" | "interrupted";
  receivedBytes: number;
  /** 未知大小时为 0。 */
  totalBytes: number;
};

/** 内置浏览器的一个标签页（宿主事实，见 `electron/main/browser/browser-host.ts`）；每个标签属于一个 Task。 */
export type BrowserTabSummary = {
  tabId: string;
  taskId: number;
  createdBy: BrowserTabCreator;
  /** 页面用 `window.open` / `target=_blank` 开出的标签记下来源标签（来源已关闭时仍保留 id）；否则为 null。 */
  openerId: string | null;
  url: string;
  title: string;
  loading: boolean;
  crashed: boolean;
  dialog: BrowserDialog | null;
};

/** 内置浏览器的完整状态（宿主事实），推送与读取都是这一份。 */
export type BrowserState = {
  tabs: BrowserTabSummary[];
  /** 各 Task 最近的下载，旧的在前。 */
  downloads: BrowserDownload[];
};

/** 浏览器面板占位区域的矩形：CSS 像素、相对窗口内容区（即 `getBoundingClientRect`）。 */
export type BrowserPlacement = { x: number; y: number; width: number; height: number };

export const BROWSER_NAVIGATION_ACTIONS = ["back", "forward", "reload"] as const;

/** 导航目标：打开地址，或后退 / 前进 / 刷新（用户地址栏与 Agent 的 `browser.navigate` 共用）。 */
export type BrowserNavigation = { url: string } | { action: (typeof BROWSER_NAVIGATION_ACTIONS)[number] };

/** 两个标签上限；事实源是后端配置中心，每次请求随参数带来，宿主不另存配置。 */
export type BrowserTabLimits = { perTask: number; total: number };

/**
 * 新开标签需要的后端事实（后端按 Task 解析：`GET /tasks/{id}/browser-context`，Agent 侧经工具参数带来）：
 * 上限，以及这个 Task 所在工作区的下载目录（本机绝对路径）。下载目录在标签创建时定下，页面开出的标签沿用来源标签的。
 */
export type BrowserTabContext = { limits: BrowserTabLimits; downloadDir: string };

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
  browser: {
    state(): Promise<BrowserState>;
    /** 订阅状态变化（每次推送完整状态），返回取消订阅函数。 */
    onStateChanged(listener: (state: BrowserState) => void): () => void;
    /**
     * 在 Task 里新开标签（可直接打开地址），返回标签 id。`context` 由后端按 Task 给出：宿主不存配置，
     * 超出上限时抛出带原因的错误。
     */
    open(taskId: number, context: BrowserTabContext, url?: string): Promise<string>;
    /**
     * 把标签的网页画面放到页面的这个矩形上；`null` 收起。同一时刻只有一个标签显示，放上新的会收起旧的。
     * 网页是叠在页面之上的原生画面，不参与 CSS 层叠。
     */
    place(tabId: string, placement: BrowserPlacement | null): Promise<void>;
    /** 当前画面的一帧（JPEG data URL），弹层遮住网页区域时先放这张图再收起画面。 */
    freeze(tabId: string): Promise<string>;
    navigate(tabId: string, navigation: BrowserNavigation): Promise<void>;
    close(tabId: string): Promise<void>;
    /** 回答标签上 `dialogId` 这个弹窗；它已不是当前弹窗（被 Agent 答过、换成了新的）时什么也不做。 */
    answerDialog(tabId: string, dialogId: number, answer: BrowserDialogAnswer): Promise<void>;
    /** 在系统文件管理器里显示某次下载（按 id 寻址，不接受任意路径）。 */
    revealDownload(downloadId: string): Promise<void>;
  };
  files: {
    /**
     * 用户经选择框、粘贴或拖放交给页面的 `File` 的本机路径；非本机文件（如剪贴板截图）为空串。
     * 渲染进程只能对自己手里的 `File` 取路径，无法借它探测任意路径。
     */
    pathOf(file: File): string;
  };
};
