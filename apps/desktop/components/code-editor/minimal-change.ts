import type { ChangeSpec, EditorState } from "@codemirror/state";

/**
 * 把文档换成 `next` 时真正要改的那一段（去掉相同的开头与结尾）；内容没变时为 null。内容被外部改写（Agent 改了
 * 正在看的文件、配置从服务端回来）时用它代替整篇替换，没变的部分保留着色、选区与折叠，不从头重算。
 *
 * 先按编辑器的换行规则归一 `next`（`state.toText`）再比：文档里的换行一律是 `\n`，而文件可能是 `\r\n`，
 * 直接逐字比会切在 `\r\n` 中间，插入一个孤立的 `\r`，凭空多出一行。
 */
export function minimalChange(state: EditorState, next: string): ChangeSpec | null {
  const current = state.doc.toString();
  const target = state.toText(next).toString();
  if (current === target) return null;
  const limit = Math.min(current.length, target.length);
  let start = 0;
  while (start < limit && current.charCodeAt(start) === target.charCodeAt(start)) start++;
  let end = 0;
  while (end < limit - start && current.charCodeAt(current.length - 1 - end) === target.charCodeAt(target.length - 1 - end)) end++;
  return { from: start, to: current.length - end, insert: target.slice(start, target.length - end) };
}
