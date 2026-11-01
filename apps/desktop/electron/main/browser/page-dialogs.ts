import type { IpcMainEvent } from "electron";

import { BROWSER_DIALOG_TYPES, type BrowserDialog, type BrowserDialogAnswer } from "../../shared/host-contract";
import type { BrowserPageDialogReply, BrowserPageDialogRequest } from "../../shared/browser-page-bridge";

const MESSAGE_MAX_CHARS = 2_000;
/** 宿主内单调递增：作答按 id 指明是哪一个弹窗。 */
let lastDialogId = 0;

export type DialogOpening =
  | { dialog: BrowserDialog }
  /** 没有登记：类型不认识，或这个标签已有一个待答的（另一个框架同时弹窗）。已按取消答复。 */
  | { refused: "unknown-type" | "busy" };

/**
 * 一个标签上待答的 JS 弹窗：唯一事实源。页面经网页 preload 的同步 IPC 发来请求后停住，直到这里答复
 * （`event.returnValue`）。作答必须指明弹窗 id，不是当前那个就不生效：用户与 Agent 都能作答，过时的作答
 * （对方刚答完、页面紧接着弹出新的）不能落到新弹窗上。导航、关闭标签、渲染进程退出时按取消收尾（`dismiss`）。
 *
 * 页面停住影响的是整个渲染进程，「这个标签有弹窗」只是其中一面；同进程其他标签的 CDP 闸门由 `BrowserHost`
 * 按进程归组判断，这里只管本标签的弹窗。
 */
export class PageDialogs {
  private pending: { dialog: BrowserDialog; event: IpcMainEvent } | null = null;
  private waiters: (() => void)[] = [];

  get current(): BrowserDialog | null {
    return this.pending?.dialog ?? null;
  }

  open(event: IpcMainEvent, request: BrowserPageDialogRequest): DialogOpening {
    const type = BROWSER_DIALOG_TYPES.find((item) => item === request.type);
    if (!type || this.pending) {
      event.returnValue = type === "confirm" ? false : null;
      return { refused: type ? "busy" : "unknown-type" };
    }
    const dialog: BrowserDialog = {
      id: ++lastDialogId,
      type,
      message: String(request.message).slice(0, MESSAGE_MAX_CHARS),
      defaultText: type === "prompt" ? String(request.defaultText).slice(0, MESSAGE_MAX_CHARS) : "",
    };
    this.pending = { dialog, event };
    return { dialog };
  }

  /** 回答 `id` 这个弹窗；它已不是当前弹窗时返回 null。 */
  answer(id: number, answer: BrowserDialogAnswer): BrowserDialog | null {
    return this.pending?.dialog.id === id ? this.settle(answer) : null;
  }

  /** 不论哪个，按取消收尾当前弹窗；没有时返回 null。 */
  dismiss(): BrowserDialog | null {
    return this.pending ? this.settle({ accept: false }) : null;
  }

  /** 没有待答的弹窗时立即完成，否则在它被答复时完成。 */
  settled(): Promise<void> {
    if (!this.pending) return Promise.resolve();
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  private settle(answer: BrowserDialogAnswer): BrowserDialog {
    const { dialog, event } = this.pending!;
    this.pending = null;
    event.returnValue = reply(dialog, answer);
    for (const resolve of this.waiters.splice(0)) resolve();
    return dialog;
  }
}

function reply(dialog: BrowserDialog, { accept, text }: BrowserDialogAnswer): BrowserPageDialogReply {
  if (dialog.type === "alert") return null;
  if (dialog.type === "confirm") return accept;
  return accept ? (text ?? dialog.defaultText) : null;
}
