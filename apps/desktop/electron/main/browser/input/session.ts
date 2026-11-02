import { HostMethodError } from "../../host-link/methods";
import type { BrowserDialog, BrowserDialogAnswer } from "../../../shared/host-contract";
import { browserError } from "../errors";
import type { PageNode } from "../ref-registry";
import type { Box } from "./element";

/** 页面打开、等 Agent 回答的选文件框：要填的文件输入框，以及它能否收多个文件。 */
export type FileChooser = { node: PageNode; multiple: boolean };

/** `BrowserTab` 交给动作模块的最小能力；不为它单独引入接口类型。 */
export type PageSession = {
  /** `sessionId`：节点所在的跨进程子框架会话（见 `PageNode`）；合成输入一律发主会话。 */
  cdp<T = unknown>(method: string, params?: Record<string, unknown>, options?: { browserSide?: boolean; sessionId?: string }): Promise<T>;
  /** 订阅一个 CDP 事件，返回退订函数。 */
  on<T>(method: string, handler: (params: T) => void): () => void;
  resolveRef(ref: string): PageNode;
  /** 会话里的视口坐标 → 主视口坐标的偏移（跨进程子框架；主会话为 0）。 */
  frameOffset(sessionId: string | undefined): Promise<{ x: number; y: number }>;
  /** 动作可能触发导航：等主框架加载结束（有时限，超时不算失败）。 */
  settle(): Promise<void>;
  /** 页面上已生效的视口（CSS 像素）：坐标落点的范围。 */
  viewport(): { width: number; height: number };
  /** CSS 像素 → CDP 输入坐标（视图坐标）：面板缩放显示视口时两者不同。 */
  toInput(point: { x: number; y: number }): { x: number; y: number };
  /** 页面停着的原因（本标签或同进程别的标签有待答弹窗）；没停时为 null。 */
  paused(): HostMethodError | null;
  /** 本标签待答的弹窗。 */
  dialog(): BrowserDialog | null;
  answerDialog(id: number, answer: BrowserDialogAnswer): void;
  /** Agent 的动作让页面打开、还没作废的选文件框。 */
  fileChooser(): FileChooser | null;
  /** 给人看要操作哪里：标签挂在面板上时才调用 `locate` 量位置并画框；从不抛错。 */
  highlight(locate: () => Promise<Box>): Promise<void>;
};

/** 动作的生效命令遇到弹窗：动作已生效，弹窗是它触发的。 */
export class DialogOpenedByAction extends Error {}

/**
 * 动作的生效命令：它遇到 `DIALOG_OPEN` 说明弹窗是这个动作触发的（归因规则见 `actions.ts`）。每个动作模块把
 * 自己的生效命令包进来，不包的命令遇到弹窗按失败处理。
 */
export async function effect<T>(command: Promise<T>): Promise<T> {
  try {
    return await command;
  } catch (error) {
    if (error instanceof HostMethodError && error.code === "DIALOG_OPEN") throw new DialogOpenedByAction();
    throw error;
  }
}

/** 动作序列为什么停了：标签关闭等宿主给出的原因原样带回，其余（请求被取消）按 `CANCELLED`。 */
export function stopped(signal: AbortSignal): HostMethodError {
  return signal.reason instanceof HostMethodError ? signal.reason : browserError("CANCELLED", "the call was cancelled");
}
