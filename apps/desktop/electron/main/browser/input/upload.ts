import { browserError } from "../errors";
import type { PageNode } from "../ref-registry";
import { callOn, element, onNode } from "./element";
import { effect, type FileChooser, type PageSession } from "./session";

/** 文件输入框能否收下这些文件；约定同 `callOn`（空串为可以）。 */
const FILE_INPUT_CHECK = `function (count) {
  if (!(this instanceof HTMLInputElement) || this.type !== "file") return "not a file input";
  if (this.disabled) return "disabled";
  if (count > 1 && !this.multiple) return "single";
  return "";
}`;

/**
 * 把文件交给页面：`ref` 指向文件输入框，或省略而回答 Agent 的动作让页面打开的选文件框（网站常把输入框藏起来、
 * 用按钮打开选择框，快照里没有它的 ref）。路径由后端按读文件的规则解析成绝对路径并确认存在（不存在的路径 CDP
 * 会静默放进一个空文件），这里只决定交给哪个输入框。`DOM.setFileInputFiles` 照真实选择触发 input / change。
 */
export async function upload(session: PageSession, paths: readonly string[], ref: string | undefined): Promise<void> {
  const target = ref ? await fileInput(session, ref, paths.length) : chooser(session.fileChooser(), paths.length);
  await effect(onNode(session, target, "DOM.setFileInputFiles", { files: paths }));
}

async function fileInput(session: PageSession, ref: string, count: number): Promise<PageNode> {
  const node = await element(session, ref);
  const problem = await callOn(session, node, FILE_INPUT_CHECK, [count]);
  if (problem === "not a file input") throw browserError("NO_FILE_INPUT", `${ref} is not a file input`);
  if (problem === "disabled") throw browserError("NOT_INTERACTABLE", `the file input ${ref} is disabled`);
  if (problem === "single") throw single();
  return node;
}

function chooser(pending: FileChooser | null, count: number): PageNode {
  if (!pending) throw browserError("NO_FILE_INPUT", "no file chooser is waiting for files");
  if (count > 1 && !pending.multiple) throw single();
  return pending.node;
}

function single() {
  return browserError("ACTION_FAILED", "this file input takes a single file; upload one path");
}
