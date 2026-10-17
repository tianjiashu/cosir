import type { NodeViewProps } from "@tiptap/react";
import { closeHistory } from "@tiptap/pm/history";
import { TextSelection } from "@tiptap/pm/state";

/** 删除两侧均封闭历史分组；连续插入、移除、输入可分别撤销，周围正文和空白原样保留。 */
export function removeComposerToken({ editor, getPos, node }: Pick<NodeViewProps, "editor" | "getPos" | "node">) {
  if (!editor.isEditable) return;
  const position = getPos();
  if (position === undefined) return;
  const transaction = closeHistory(editor.state.tr).delete(position, position + node.nodeSize);
  transaction.setSelection(TextSelection.near(transaction.doc.resolve(position)));
  editor.view.dispatch(transaction);
  editor.view.dispatch(closeHistory(editor.state.tr));
  editor.commands.focus();
}
