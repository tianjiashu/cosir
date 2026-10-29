import { create } from "zustand";

import { userErrorMessage } from "@/lib/errors/user-error-message";

/**
 * 对话框语气：`destructive` 删除、关闭等不可逆操作；`warning` 放弃未保存的修改；`default` 普通确认。
 * 决定标题图标的底色与主按钮样式。
 */
export type ConfirmTone = "destructive" | "warning" | "default";

export type ConfirmationRequest = {
  title: string;
  description: string;
  /** 为 true 时在说明下方加一行「此操作无法撤销。」；不要再把这句写进说明或 warning。 */
  irreversible?: boolean;
  /** 需要醒目强调的后果（会中断运行中的任务等），渲染为提示框；一般的不可撤销用 `irreversible`。 */
  warning?: string;
  confirmLabel: string;
  /** 默认「处理中…」。 */
  busyLabel?: string;
  tone?: ConfirmTone;
  /** 标题图标的语义，由 ConfirmHost 映射为具体图标，store 不依赖 React 组件。 */
  icon?: "delete" | "discard" | "restart" | "error";
  /**
   * 确认后执行的操作。执行期间对话框保持忙碌、不可关闭；失败时在框内显示错误并保持打开，
   * 用户可以重试或取消。不传则确认即关闭。
   */
  action?: () => Promise<unknown>;
  /** action 失败时的兜底文案，经 userErrorMessage 转换。 */
  failureFallback?: string;
  /**
   * 与主操作并列的另一种确认（如「一起删除」与「只删除这个」），渲染为描边按钮。执行与失败的
   * 处理同主操作；确认结果仍是 true，调用方要区分走了哪一种，就在各自的 action 里记下。
   */
  alternative?: { label: string; action: () => Promise<unknown> };
  /** 只告知、不需要选择：只有一个按钮（`confirmLabel`），没有取消；见 {@link showErrorDialog}。 */
  notice?: boolean;
};

/** 用户确认时选了哪一个：主操作或 `alternative`。 */
export type ConfirmChoice = "primary" | "alternative";

type PendingConfirmation = ConfirmationRequest & {
  id: number;
  resolve: (confirmed: boolean) => void;
};

type ConfirmState = {
  queue: PendingConfirmation[];
  /** 正在执行的那个选择；null 为空闲。 */
  busy: ConfirmChoice | null;
  error: string | null;
};

let nextId = 1;

export const useConfirmStore = create<ConfirmState>(() => ({ queue: [], busy: null, error: null }));

/**
 * 请求用户确认——取代 `window.confirm` 的唯一入口，可在组件外调用。
 *
 * 返回 Promise：用户取消为 false；确认且 action（若有）成功为 true。同一时刻只显示
 * 一个确认框，其余按请求顺序排队。需要应用根部挂载 ConfirmHost。删除与放弃修改优先用
 * {@link confirmDelete} / {@link confirmDiscard}，文案、语气与按钮在那里统一。
 */
export function requestConfirmation(request: ConfirmationRequest): Promise<boolean> {
  return new Promise((resolve) => {
    useConfirmStore.setState((state) => ({
      queue: [...state.queue, { ...request, id: nextId++, resolve }],
    }));
  });
}

type ConfirmDeleteRequest = {
  /** 被删对象的可读描述，用于标题，如 `模型配置「Demo」`。 */
  target: string;
  /** 删除带来的后果；标题已说明删什么，没有额外后果时省略。 */
  consequence?: string;
  /** 默认 true；只是移出列表、可以再加回来的删除传 false。 */
  irreversible?: boolean;
  /** 需要醒目强调的额外后果（会删除磁盘文件等）。 */
  warning?: string;
  confirmLabel?: string;
  action?: () => Promise<unknown>;
  failureFallback?: string;
  alternative?: ConfirmationRequest["alternative"];
};

/** 删除确认：标题「删除{target}？」、红色语气、主按钮「删除」、默认提示无法撤销。 */
export function confirmDelete(request: ConfirmDeleteRequest): Promise<boolean> {
  // 中英文之间留空格（「删除 MCP 服务」），与界面其余文案一致。
  const separator = /^[A-Za-z0-9]/.test(request.target) ? " " : "";
  return requestConfirmation({
    title: `删除${separator}${request.target}？`,
    description: request.consequence ?? "",
    irreversible: request.irreversible ?? true,
    warning: request.warning,
    confirmLabel: request.confirmLabel ?? "删除",
    busyLabel: "正在删除…",
    tone: "destructive",
    icon: "delete",
    action: request.action,
    failureFallback: request.failureFallback ?? "删除失败，请重试",
    alternative: request.alternative,
  });
}

/** 放弃未保存修改的确认：黄色语气、放弃图标；没有异步操作，确认即关闭。 */
export function confirmDiscard(request: { title: string; description: string; confirmLabel: string }): Promise<boolean> {
  return requestConfirmation({ ...request, tone: "warning", icon: "discard" });
}

/**
 * 告知一次失败：与确认框共用队列与外观，只有一个按钮，关闭即 resolve。用于需要用户停下来看的
 * 失败（请求未被接受、保存失败）；可以一闪而过的失败用 `notify.error`。
 */
export async function showErrorDialog(request: { title: string; message: string; actionLabel?: string }): Promise<void> {
  await requestConfirmation({
    title: request.title,
    description: request.message,
    confirmLabel: request.actionLabel ?? "知道了",
    tone: "destructive",
    icon: "error",
    notice: true,
  });
}

function settleCurrent(confirmed: boolean) {
  const [current, ...rest] = useConfirmStore.getState().queue;
  if (!current) return;
  useConfirmStore.setState({ queue: rest, busy: null, error: null });
  current.resolve(confirmed);
}

/** ConfirmHost 的「取消」。执行中不可取消（与对话框禁用关闭一致）。 */
export function cancelCurrentConfirmation() {
  if (useConfirmStore.getState().busy) return;
  settleCurrent(false);
}

/** ConfirmHost 的「确认」：执行所选操作并托管 busy / error。 */
export async function acceptCurrentConfirmation(choice: ConfirmChoice = "primary") {
  const state = useConfirmStore.getState();
  const current = state.queue[0];
  if (!current || state.busy) return;
  const action = choice === "alternative" ? current.alternative?.action : current.action;
  if (!action) {
    settleCurrent(true);
    return;
  }
  useConfirmStore.setState({ busy: choice, error: null });
  try {
    await action();
    settleCurrent(true);
  } catch (error) {
    useConfirmStore.setState({
      busy: null,
      error: userErrorMessage(error, current.failureFallback ?? "操作失败，请重试"),
    });
  }
}
