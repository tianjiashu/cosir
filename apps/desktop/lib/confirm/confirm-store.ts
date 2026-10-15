import { create } from "zustand";

import { userErrorMessage } from "@/lib/errors/user-error-message";

export type ConfirmationRequest = {
  title: string;
  description: string;
  warning?: string;
  confirmLabel: string;
  /** 默认「处理中…」。 */
  busyLabel?: string;
  tone?: "destructive" | "default";
  /** 确认图标的语义，由 ConfirmHost 映射为具体图标，store 不依赖 React 组件。 */
  icon?: "delete" | "discard" | "restart";
  /**
   * 确认后执行的操作。执行期间对话框保持忙碌、不可关闭；失败时在框内显示错误并保持打开，
   * 用户可以重试或取消。不传则确认即关闭。
   */
  action?: () => Promise<unknown>;
  /** action 失败时的兜底文案，经 userErrorMessage 转换。 */
  failureFallback?: string;
};

type PendingConfirmation = ConfirmationRequest & {
  id: number;
  resolve: (confirmed: boolean) => void;
};

type ConfirmState = {
  queue: PendingConfirmation[];
  busy: boolean;
  error: string | null;
};

let nextId = 1;

export const useConfirmStore = create<ConfirmState>(() => ({ queue: [], busy: false, error: null }));

/**
 * 请求用户确认——取代 `window.confirm` 的唯一入口，可在组件外调用。
 *
 * 返回 Promise：用户取消为 false；确认且 action（若有）成功为 true。同一时刻只显示
 * 一个确认框，其余按请求顺序排队。需要应用根部挂载 ConfirmHost。
 */
export function requestConfirmation(request: ConfirmationRequest): Promise<boolean> {
  return new Promise((resolve) => {
    useConfirmStore.setState((state) => ({
      queue: [...state.queue, { ...request, id: nextId++, resolve }],
    }));
  });
}

function settleCurrent(confirmed: boolean) {
  const [current, ...rest] = useConfirmStore.getState().queue;
  if (!current) return;
  useConfirmStore.setState({ queue: rest, busy: false, error: null });
  current.resolve(confirmed);
}

/** ConfirmHost 的「取消」。执行中不可取消（与对话框禁用关闭一致）。 */
export function cancelCurrentConfirmation() {
  if (useConfirmStore.getState().busy) return;
  settleCurrent(false);
}

/** ConfirmHost 的「确认」：执行 action 并托管 busy / error。 */
export async function acceptCurrentConfirmation() {
  const state = useConfirmStore.getState();
  const current = state.queue[0];
  if (!current || state.busy) return;
  if (!current.action) {
    settleCurrent(true);
    return;
  }
  useConfirmStore.setState({ busy: true, error: null });
  try {
    await current.action();
    settleCurrent(true);
  } catch (error) {
    useConfirmStore.setState({
      busy: false,
      error: userErrorMessage(error, current.failureFallback ?? "操作失败，请重试"),
    });
  }
}
