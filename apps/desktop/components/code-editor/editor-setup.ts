import { autocompletion, closeBrackets } from "@codemirror/autocomplete";
import { history } from "@codemirror/commands";
import { bracketMatching, foldGutter, indentOnInput } from "@codemirror/language";
import { highlightSelectionMatches, search } from "@codemirror/search";
import { EditorState } from "@codemirror/state";
import {
  crosshairCursor, drawSelection, dropCursor, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars,
  lineNumbers, rectangularSelection,
} from "@codemirror/view";

import { findHighlights } from "./editor-find";
import { editorKeymap } from "./editor-keymap";

/**
 * 文件阅读器与 Markdown 配置编辑器共用的 CodeMirror 基础装配，取代 `codemirror` 包的 `basicSetup`。
 *
 * 编辑能力沿用 `basicSetup`（行号、折叠栏、括号匹配、自动闭合、补全、多选区等），有三处不同：
 * - 键位：`basicSetup` 自带的键表不经快捷键登记表，提示会写错、平台规则管不到；这里全部来自 `editorKeymap`。
 * - 查找与替换：用应用的查找条（`findHighlights` 是它的高亮层，`search()` 只提供替换用的查询状态，不装
 *   CodeMirror 的搜索面板）。
 * - 语法着色：不装 `basicSetup` 的 Lezer 配色，由使用方装 `editorHighlighting`（Shiki，与代码块、diff 同源）。
 */
export const editorSetup = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightSpecialChars(),
  history(),
  foldGutter(),
  drawSelection(),
  dropCursor(),
  EditorState.allowMultipleSelections.of(true),
  indentOnInput(),
  bracketMatching(),
  closeBrackets(),
  autocompletion({ defaultKeymap: false }),
  rectangularSelection(),
  crosshairCursor(),
  highlightActiveLine(),
  highlightSelectionMatches(),
  search(),
  findHighlights,
  editorKeymap,
];
