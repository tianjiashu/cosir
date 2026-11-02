import { HostMethodError } from "../../host-link/methods";
import { browserError } from "../errors";
import type { PageAction } from "../params";
import { highlightTarget } from "./highlight";
import { pressKey, typeText } from "./keyboard";
import { scroll, select } from "./page-functions";
import { click, drag, hover } from "./pointer";
import { DialogOpenedByAction, stopped, type PageSession } from "./session";
import { upload } from "./upload";
import { waitFor } from "./wait";

/**
 * 在页面上执行一组动作（点击、悬停、拖动、输入、按键、选择、滚动、上传、等待、回答弹窗）。只经 CDP 合成输入；
 * Agent 与用户输入的区分见 `BrowserTab.act`。各类动作的实现按输入方式分在本目录（鼠标、键盘、页面函数、上传、
 * 等待），这里只负责调度、批次状态与弹窗归因。
 *
 * 动作按序执行、遇错即停：已完成的动作不回滚，结果里说明停在哪一步，调用方随后照常返回新快照，模型能直接
 * 接着做。
 *
 * 弹窗：页面停住时（本标签或同进程别的标签在弹窗）CDP 命令回不来，以 `DIALOG_OPEN` 失败。只有动作在它的
 * 「生效命令」（`effect`：点击与拖动的按下、移动与松开，悬停的移动，输入与按键、选择、滚动、上传）上遇到它，
 * 才说明是动作自己触发的弹窗、动作已生效，按完成处理并记下这个弹窗；其余位置（定位元素、`wait_for` 轮询……）
 * 遇到它就是失败——页面自己在别的时机弹了窗，动作并没有发生。`dialog` 动作只回答指明 id 的弹窗，或本批前面的
 * 动作弹出的那个，所以「点删除 + 确认」能在同一批里完成，又不会把确认答给一个没看见的弹窗。
 */

/** 一批动作执行到哪一步、停在哪（`runActions` 的结果）。 */
export type BatchResult = {
  completed: number;
  failure: { index: number; code: string; message: string } | null;
};

/** 交给 Agent 与工具卡片的结果：另附每个动作的目标说明（`targets.ts` 的 `describeTarget`），与动作一一对应。 */
export type ActResult = BatchResult & { targets: (string | null)[] };

/** 本批执行中的共享状态：前面的动作弹出的弹窗（`dialog` 动作省略 id 时只能回答它）。 */
type Batch = { openedDialog: number | null };

export async function runActions(session: PageSession, actions: readonly PageAction[], signal: AbortSignal): Promise<BatchResult> {
  const batch: Batch = { openedDialog: null };
  for (const [index, action] of actions.entries()) {
    if (signal.aborted) return { completed: index, failure: { index, ...describe(stopped(signal)) } };
    const paused = session.paused();
    if (paused && action.type !== "dialog") return { completed: index, failure: { index, code: paused.code, message: paused.message } };
    try {
      await highlightTarget(session, action);
      await run(session, action, signal, batch);
      await session.settle();
    } catch (error) {
      if (error instanceof DialogOpenedByAction) {
        batch.openedDialog = session.dialog()?.id ?? null;
        continue;
      }
      return { completed: index, failure: { index, ...describe(error) } };
    }
  }
  return { completed: actions.length, failure: null };
}

/** 按类型分派；每种动作都必须有分支——漏了会在编译期报错，而不是静默什么都不做却按完成上报。 */
function run(session: PageSession, action: PageAction, signal: AbortSignal, batch: Batch): Promise<void> {
  switch (action.type) {
    case "click":
      return click(session, action.target, action);
    case "hover":
      return hover(session, action.target);
    case "drag":
      return drag(session, action.start, action.end);
    case "type":
      return typeText(session, action.ref, action.text, action);
    case "press":
      return pressKey(session, action.key, action.modifiers, action.ref);
    case "select":
      return select(session, action.ref, action.values);
    case "scroll":
      return scroll(session, action.direction, action.ref);
    case "wait_for":
      // 省略时等 5 秒（与参数说明一致）；上限由契约 schema 保证。
      return waitFor(session, action, action.seconds ?? 5, signal);
    case "upload":
      return upload(session, action.paths, action.ref);
    case "dialog":
      return answer(session, action, batch);
    default:
      return unreachable(action);
  }
}

async function answer(session: PageSession, action: Extract<PageAction, { type: "dialog" }>, batch: Batch): Promise<void> {
  const current = session.dialog();
  if (!current) throw browserError("NO_DIALOG", "this tab is not showing a dialog");
  const expected = action.id ?? batch.openedDialog;
  if (expected !== current.id) {
    const asked = action.id === undefined ? "pass its id (only a dialog that an earlier action in this call is known to have opened can be answered without one)" : `not #${action.id}`;
    throw browserError("DIALOG_MISMATCH", `the tab is showing dialog #${current.id}; ${asked}`);
  }
  session.answerDialog(current.id, { accept: action.accept, text: action.text });
}

function unreachable(action: never): never {
  throw new Error(`unhandled browser action ${JSON.stringify(action)}`);
}

function describe(error: unknown): { code: string; message: string } {
  if (error instanceof HostMethodError) return { code: error.code, message: error.message };
  return { code: "ACTION_FAILED", message: error instanceof Error ? error.message : String(error) };
}
