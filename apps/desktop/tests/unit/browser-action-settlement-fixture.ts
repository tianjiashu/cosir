/**
 * 动作结算矩阵的测试替身：只按生产 `CdpSession/BrowserTab` 的语义搭一个仓库内替身，
 * 不 mock 被测模块本身（`BrowserOperation`、`runActions`、`input/*` 都是生产代码）。
 *
 * 关键点（与生产 `cdp-session.ts:81-108` 的顺序一致）：
 *   1. signal 取消检查
 *   2. 弹窗暂停闸门（browserSide 命令不过闸门）→ 未派发就失败，不置 effectStarted
 *   3. onDispatch（登记输入来源）
 *   4. sendCommand（这里对应 `dispatch`：可能返回已拒绝的 Promise = 命令已派发后 Chromium 报错）
 *   5. onCommand（`BrowserOperation.send` 在这里置 effectStarted）
 *
 * 这样可以区分「命令已派发后失败」（可能已生效）与「闸门在派发前拒绝」（确定没生效）——
 * 上一轮复核的 fixture 在暂停路径上顺序不准确，本 fixture 用于把每一条失败路径都按真实顺序复现。
 */
import { HostMethodError } from "@/electron/main/host-link/method-registry";
import { BrowserOperation, type FileChooser } from "@/electron/main/browser/operation";
import type { BrowserDialog, BrowserDialogAnswer } from "@/electron/shared/host-contract";
import type { BrowserTab } from "@/electron/main/browser/browser-tab";
import type { SendOptions } from "@/electron/main/browser/cdp-session";

export type Call = { method: string; params: Record<string, unknown>; sessionId?: string; phase: "dispatched" | "gate-rejected" };

export type FixtureConfig = {
  /** 页内固定函数按片段匹配的返回值（命中第一个片段），返回 Error 表示页面抛异常。 */
  answers?: Record<string, unknown>;
  /** 命令已派发后失败（Chromium 报错）→ effectStarted 会置位。 */
  failAfterDispatch?: (method: string, params: Record<string, unknown>, counts: Record<string, number>) => Error | undefined;
  /** 闸门在派发前拒绝（模拟页面在动作前被弹窗停住 / 已取消）→ effectStarted 不置位。 */
  rejectBeforeDispatch?: (method: string, params: Record<string, unknown>, counts: Record<string, number>) => Error | undefined;
  /** 命令派发后页面弹出弹窗（命令以 DIALOG_OPEN 失败，但已派发）。 */
  dialogOnDispatch?: (method: string, params: Record<string, unknown>, counts: Record<string, number>) => boolean;
  /** 动作开始时页面已停住（本标签或同进程别的标签有弹窗）。 */
  pausedBy?: HostMethodError | null;
  initialDialog?: BrowserDialog | null;
  fileChooser?: FileChooser | null;
  /** HTML5 拖放：拦截开启后，按下并移动时 Chromium 会发 Input.dragIntercepted，这里用这个数据模拟。 */
  dragData?: unknown;
  /** 标签挂在面板上：高亮链会真的量位置（经 session.cdp 发只读命令）。 */
  shown?: boolean;
};

export type Fixture = {
  session: BrowserOperation;
  calls: Call[];
  signal: AbortSignal;
  abort: () => void;
  answered: [number, BrowserDialogAnswer][];
  dialog: () => BrowserDialog | null;
};

export function makeSession(config: FixtureConfig = {}): Fixture {
  const calls: Call[] = [];
  const counts: Record<string, number> = {};
  const answered: [number, BrowserDialogAnswer][] = [];
  const listeners = new Map<string, Set<(params: never) => void>>();
  let intercepting = false;
  let dialog: BrowserDialog | null = config.initialDialog ?? null;

  const pausedError = () =>
    config.pausedBy ??
    (dialog
      ? new HostMethodError("DIALOG_OPEN", `the page is paused: it is showing a ${dialog.type} dialog (#${dialog.id})`)
      : null);

  const answerOf = (text: string) =>
    Object.entries(config.answers ?? {}).find(([fragment]) => text.includes(fragment))?.[1] ?? "";

  /** 等价于 CdpSession 里 debugger.sendCommand 之后的响应：返回 Promise，命令已「派发」。 */
  const dispatch = (method: string, params: Record<string, unknown>): Promise<unknown> => {
    const n = (counts[method] = (counts[method] ?? 0) + 1);
    counts["__total"] = (counts["__total"] ?? 0) + 1;

    if (config.dialogOnDispatch?.(method, params, counts)) {
      dialog = { id: 7, type: "confirm", message: "delete?", defaultText: "" };
      return Promise.reject(pausedError() ?? new HostMethodError("DIALOG_OPEN", "paused"));
    }
    const failure = config.failAfterDispatch?.(method, params, counts);
    if (failure) return Promise.reject(failure);

    if (method === "Input.setInterceptDrags") intercepting = params.enabled === true;
    if (intercepting && config.dragData !== undefined && method === "Input.dispatchMouseEvent" && params.buttons === 1) {
      for (const handler of listeners.get("Input.dragIntercepted") ?? []) handler({ data: config.dragData } as never);
    }

    if (method === "Runtime.callFunctionOn" || method === "Runtime.evaluate") {
      const text = String(params.functionDeclaration ?? params.expression);
      const value = answerOf(text);
      if (value instanceof Error) {
        return Promise.resolve({
          result: { value: undefined, type: "object" },
          exceptionDetails: { text: "Uncaught", exception: { description: `${value.name}: ${value.message}\n    at <anonymous>:1:1` } },
        });
      }
      return Promise.resolve({ result: { type: "string", value } });
    }
    if (method === "DOM.resolveNode") return Promise.resolve({ object: { objectId: `obj-${n}` } });
    if (method === "DOM.getBoxModel") return Promise.resolve({ model: { border: [0, 0, 10, 0, 10, 20, 0, 20] } });
    if (method === "Page.getLayoutMetrics") {
      return Promise.resolve({
        layoutViewport: { clientWidth: 100 },
        cssLayoutViewport: { clientWidth: 100 },
        cssVisualViewport: { pageX: 0, pageY: 0, clientWidth: 100, clientHeight: 50, scale: 1 },
        cssContentSize: { width: 100, height: 50 },
      });
    }
    return Promise.resolve({});
  };

  const tab = {
    taskId: 1,
    tabId: "t1",
    documentGeneration: 0,
    contents: { isDestroyed: () => false },
    noteAgentInput: () => undefined,
    operationChanged: () => undefined,
    operationSettled: () => undefined,
    refLabel: () => undefined,
    session: {
      send: <T,>(method: string, params: Record<string, unknown>, options: SendOptions): Promise<T> => {
        // 1. signal 检查
        options.signal?.throwIfAborted();
        // 2. 暂停闸门：在派发之前
        if (!options.browserSide) {
          const paused = pausedError();
          if (paused) {
            calls.push({ method, params, sessionId: options.sessionId, phase: "gate-rejected" });
            return Promise.reject(paused);
          }
        }
        const rejected = config.rejectBeforeDispatch?.(method, params, counts);
        if (rejected) {
          calls.push({ method, params, sessionId: options.sessionId, phase: "gate-rejected" });
          return Promise.reject(rejected);
        }
        // 3. onDispatch
        options.onDispatch?.();
        // 4. sendCommand
        calls.push({ method, params, sessionId: options.sessionId, phase: "dispatched" });
        const raw = dispatch(method, params) as Promise<T>;
        // 5. onCommand（BrowserOperation 在这里置 effectStarted）
        options.onCommand?.(raw);
        return raw;
      },
    },
  } as unknown as BrowserTab;

  const controller = new AbortController();
  const session = new BrowserOperation(tab, controller.signal);
  Object.assign(session, {
    resolveRef: (ref: string) => {
      if (ref === "e404") throw new HostMethodError("REF_STALE", `ref ${ref} is not on the current page; take a new snapshot`);
      return ref.startsWith("f") ? { sessionId: "S1", backendNodeId: Number(ref.slice(1)) } : { backendNodeId: Number(ref.slice(1)) };
    },
    frameOffset: async (sessionId: string | undefined) => (sessionId === "S1" ? { x: 100, y: 200 } : { x: 0, y: 0 }),
    on: (method: string, handler: (params: never) => void) => {
      const set = listeners.get(method) ?? new Set();
      set.add(handler);
      listeners.set(method, set);
      return () => set.delete(handler);
    },
    settle: async () => undefined,
    toInput: ({ x, y }: { x: number; y: number }) => ({ x, y }),
    geometry: async () => ({ visual: { x: 0, y: 0, width: 100, height: 50 }, deviceRatio: 1, pageScale: 1, content: { width: 100, height: 50 } }),
    fileChooser: () => config.fileChooser ?? null,
    highlight: async (locate: () => Promise<unknown>) => {
      if (!config.shown) return;
      try { await locate(); } catch { /* 生产 highlighter.show 自己吞掉 */ }
    },
    paused: () => pausedError(),
    dialog: () => dialog,
    answerDialog: (id: number, reply: BrowserDialogAnswer) => {
      if (dialog?.id !== id) return;
      dialog = null;
      answered.push([id, reply]);
    },
  });

  return { session, calls, signal: controller.signal, abort: () => controller.abort(), answered, dialog: () => dialog };
}

