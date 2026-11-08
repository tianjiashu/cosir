import { HostMethodError } from "../../host-link/method-registry";
import { browserError } from "../errors";
import type { PageAction } from "../params";
import { highlightTarget } from "./highlight";
import { pressKey, typeText } from "./keyboard";
import { scroll, select } from "./page-functions";
import { click, drag, hover } from "./pointer";
import type { BrowserOperation } from "../operation";
import { describeTarget } from "./targets";
import { upload } from "./upload";
import { waitFor } from "./wait";

/** 执行事实与页面观察分别结算。unknown 表示命令可能已经生效，不能据此重放。 */
export type ActionStep = {
  target: string | null;
  status: "done" | "failed" | "skipped" | "unknown";
  error: { code: string; message: string } | null;
};
export type ActResult = { steps: ActionStep[]; interruption: { code: string; message: string } | null };
type Batch = { openedDialog: number | null };

/** 每步先记录执行事实再等待页面。观察失败只停止后续步骤，不改写已完成步骤。 */
export async function runActions(session: BrowserOperation, actions: readonly PageAction[], signal: AbortSignal): Promise<ActResult> {
  const batch: Batch = { openedDialog: null };
  const result: ActResult = {
    steps: actions.map((action) => ({ target: describeTarget(action, (ref) => session.tab.refLabel(ref)), status: "skipped", error: null })),
    interruption: null,
  };
  for (const [index, action] of actions.entries()) {
    const step = result.steps[index];
    session.beginStep();
    try {
      signal.throwIfAborted();
      const paused = session.paused();
      if (paused && action.type !== "dialog") throw paused;
      await highlightTarget(session, action);
      await run(session, action, signal, batch);
      step.status = "done";
    } catch (error) {
      step.status = session.effectStarted ? "unknown" : "failed";
      step.error = describe(error);
      result.interruption = step.error;
      // 未知输入后只允许紧接着处理其弹窗，不继续其它业务动作。
      batch.openedDialog = session.dialog()?.id ?? null;
      if (session.effectStarted && batch.openedDialog !== null && actions[index + 1]?.type === "dialog" && !signal.aborted) continue;
      break;
    }
    try { await session.settle(); }
    catch (error) { result.interruption = describe(error); break; }
    if (result.interruption) break;
  }
  return result;
}

/** 按类型分派；每种动作都必须有分支——漏了会在编译期报错，而不是静默什么都不做却按完成上报。 */
function run(session: BrowserOperation, action: PageAction, signal: AbortSignal, batch: Batch): Promise<void> {
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

async function answer(session: BrowserOperation, action: Extract<PageAction, { type: "dialog" }>, batch: Batch): Promise<void> {
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
