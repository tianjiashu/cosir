import { EditorSelection, type EditorState, type TransactionSpec } from "@codemirror/state";
import { EditorView } from "codemirror";

type InlineMarkdownMarker = "**" | "*" | "~~" | "`";
type BlockMarkdownKind = "bullet" | "ordered" | "quote";

const LIST_LINE_PATTERN = /^(\s*)(?:(?:[-*+])\s+|(?:\d+[.)])\s+|>\s?)/;

function dispatchTransaction(view: EditorView, transaction: TransactionSpec | null): boolean {
  if (!transaction) return false;
  view.dispatch({ ...transaction, userEvent: "input", scrollIntoView: true });
  return true;
}

function getTouchedLines(state: EditorState): Array<{ from: number; to: number; text: string }> {
  const lineNumbers = new Set<number>();
  for (const range of state.selection.ranges) {
    const firstLine = state.doc.lineAt(range.from);
    const lastPosition = range.to > range.from && range.to === state.doc.lineAt(range.to).from
      ? range.to - 1
      : range.to;
    const lastLine = state.doc.lineAt(lastPosition);
    for (let lineNumber = firstLine.number; lineNumber <= lastLine.number; lineNumber += 1) {
      lineNumbers.add(lineNumber);
    }
  }
  return [...lineNumbers]
    .sort((left, right) => left - right)
    .map((lineNumber) => {
      const line = state.doc.line(lineNumber);
      return { from: line.from, to: line.to, text: line.text };
    });
}

function inlineTransaction(state: EditorState, marker: InlineMarkdownMarker): TransactionSpec {
  return state.changeByRange((range) => {
    const selectedText = state.sliceDoc(range.from, range.to);
    const before = state.sliceDoc(Math.max(0, range.from - marker.length), range.from);
    const after = state.sliceDoc(range.to, range.to + marker.length);

    if (
      selectedText.length >= marker.length * 2 &&
      selectedText.startsWith(marker) &&
      selectedText.endsWith(marker)
    ) {
      const unwrappedText = selectedText.slice(marker.length, -marker.length);
      return {
        changes: { from: range.from, to: range.to, insert: unwrappedText },
        range: EditorSelection.range(range.from, range.from + unwrappedText.length),
      };
    }

    if (before === marker && after === marker) {
      return {
        changes: {
          from: range.from - marker.length,
          to: range.to + marker.length,
          insert: selectedText,
        },
        range: EditorSelection.range(
          range.from - marker.length,
          range.to - marker.length,
        ),
      };
    }

    if (selectedText) {
      return {
        changes: {
          from: range.from,
          to: range.to,
          insert: `${marker}${selectedText}${marker}`,
        },
        range: EditorSelection.range(
          range.from + marker.length,
          range.to + marker.length,
        ),
      };
    }

    return {
      changes: { from: range.from, insert: `${marker}${marker}` },
      range: EditorSelection.cursor(range.from + marker.length),
    };
  });
}

/**
 * 对当前选择应用或移除行内 Markdown 标记。
 *
 * 该函数只生成 CodeMirror 事务，不直接修改编辑器状态；调用方应将返回值交给
 * `EditorState.update` 或 `EditorView.dispatch`。有选中文本时包裹文本，没有选择时
 * 插入一对标记并把光标放在标记中间，重复执行会移除已有标记。
 */
export function toggleInlineMarkdown(
  state: EditorState,
  marker: InlineMarkdownMarker,
): TransactionSpec {
  return inlineTransaction(state, marker);
}

function blockPrefix(kind: BlockMarkdownKind, line: string): string {
  const indentation = line.match(/^\s*/)?.[0] ?? "";
  if (kind === "ordered") return `${indentation}1. `;
  if (kind === "quote") return `${indentation}> `;
  return `${indentation}- `;
}

function hasBlockPrefix(kind: BlockMarkdownKind, line: string): boolean {
  if (kind === "ordered") return /^\s*\d+[.)]\s+/.test(line);
  if (kind === "quote") return /^\s*>\s?/.test(line);
  return /^\s*[-*+]\s+/.test(line);
}

function removeBlockPrefix(kind: BlockMarkdownKind, line: string): string {
  if (kind === "ordered") return line.replace(/^(\s*)\d+[.)]\s+/, "$1");
  if (kind === "quote") return line.replace(/^(\s*)>\s?/, "$1");
  return line.replace(/^(\s*)[-*+]\s+/, "$1");
}

/**
 * 对当前选择涉及的段落应用或移除块级 Markdown 前缀。
 *
 * 该函数只生成事务，不负责保存或触发业务回调。选区涉及的所有行都已有目标前缀时
 * 会统一移除；否则为普通行添加前缀，并保留原有缩进。编号列表会从 1 开始连续编号。
 */
export function toggleBlockMarkdown(
  state: EditorState,
  kind: BlockMarkdownKind,
): TransactionSpec {
  const lines = getTouchedLines(state);
  const shouldRemove = lines.length > 0 && lines.every((line) => hasBlockPrefix(kind, line.text));
  let orderedNumber = 1;
  const changes = lines.map((line) => {
    if (shouldRemove) {
      return { from: line.from, to: line.to, insert: removeBlockPrefix(kind, line.text) };
    }

    const prefix = kind === "ordered" ? blockPrefix(kind, line.text).replace("1. ", `${orderedNumber}. `) : blockPrefix(kind, line.text);
    orderedNumber += kind === "ordered" ? 1 : 0;
    const existingPrefix = line.text.match(LIST_LINE_PATTERN)?.[0];
    if (existingPrefix) {
      return { from: line.from, to: line.from + existingPrefix.length, insert: prefix };
    }
    return { from: line.from, insert: prefix };
  });
  const changeSet = state.changes(changes);
  return { changes: changeSet, selection: state.selection.map(changeSet) };
}

/**
 * 将选中文本转换为 Markdown 链接。
 *
 * 有选区时保留选区作为链接文本，并选中 URL 占位符；无选区时插入空链接并选中 URL。
 * 该函数不访问网络，也不校验链接地址。
 */
export function insertMarkdownLink(state: EditorState): TransactionSpec {
  return state.changeByRange((range) => {
    const selectedText = state.sliceDoc(range.from, range.to);
    const replacement = `[${selectedText}](url)`;
    const urlFrom = range.from + selectedText.length + 3;
    return {
      changes: { from: range.from, to: range.to, insert: replacement },
      range: EditorSelection.range(urlFrom, urlFrom + 3),
    };
  });
}

function listContinuationPrefix(line: string): string | null {
  const match = line.match(/^(\s*)([-+*]|\d+[.)]|>)\s+/);
  if (!match) return null;
  const indentation = match[1];
  const marker = match[2];
  if (/^\d/.test(marker)) {
    const number = Number.parseInt(marker, 10);
    const punctuation = marker.endsWith(")") ? ")" : ".";
    return `${indentation}${number + 1}${punctuation} `;
  }
  return `${indentation}${marker} `;
}

/**
 * 在列表或引用行按 Enter 时生成下一行的 Markdown 前缀。
 *
 * 非列表行返回 `null`，交由 CodeMirror 默认 Enter 行为处理；空列表项会退出列表，
 * 非空列表项会延续前缀，编号列表的序号自动加一。
 */
export function continueMarkdownList(state: EditorState): TransactionSpec | null {
  const range = state.selection.main;
  if (!range.empty) return null;
  const line = state.doc.lineAt(range.head);
  const prefix = listContinuationPrefix(line.text);
  if (!prefix || range.head < line.from + prefix.length) return null;

  const content = line.text.slice(prefix.length);
  if (!content.trim() && range.head === line.to) {
    return { changes: { from: line.from, to: line.to, insert: "\n" } };
  }
  return {
    changes: { from: range.head, insert: `\n${prefix}` },
    selection: EditorSelection.cursor(range.head + prefix.length + 1),
  };
}

/**
 * 为选中的列表行增加或减少一级缩进。
 *
 * 只有选区涉及列表行时才会生成事务；普通文本中的 Tab 仍交给 CodeMirror 默认行为，
 * 避免改变用户原本的缩进习惯。
 */
export function indentMarkdownList(state: EditorState, outdent = false): TransactionSpec | null {
  const lines = getTouchedLines(state).filter((line) => LIST_LINE_PATTERN.test(line.text));
  if (!lines.length) return null;

  const changes = lines.map((line) => {
    if (outdent) {
      const whitespace = line.text.match(/^(\s*)/)?.[1] ?? "";
      const removeLength = whitespace.endsWith("\t") ? 1 : Math.min(2, whitespace.length);
      return { from: line.from, to: line.from + removeLength, insert: "" };
    }
    return { from: line.from, insert: "  " };
  });
  const changeSet = state.changes(changes);
  return { changes: changeSet, selection: state.selection.map(changeSet) };
}

/**
 * 处理 Markdown 编辑器的快捷键事件。
 *
 * 返回 `true` 表示事件已被编辑器消费，调用方无需再次执行浏览器默认行为；返回
 * `false` 表示交给 CodeMirror 其余 keymap 处理。只读状态下不修改文档。
 */
export function handleMarkdownKeydown(view: EditorView, event: KeyboardEvent): boolean {
  if (view.state.readOnly) return false;

  const modifier = event.metaKey || event.ctrlKey;
  const key = event.key.toLowerCase();
  const code = event.code;
  let transaction: TransactionSpec | null = null;

  if (modifier && !event.altKey) {
    if (key === "b" && !event.shiftKey) transaction = toggleInlineMarkdown(view.state, "**");
    else if (key === "i" && !event.shiftKey) transaction = toggleInlineMarkdown(view.state, "*");
    else if (key === "x" && event.shiftKey) transaction = toggleInlineMarkdown(view.state, "~~");
    else if (key === "k" && !event.shiftKey) transaction = insertMarkdownLink(view.state);
    else if (key === "`" && !event.shiftKey) transaction = toggleInlineMarkdown(view.state, "`");
    else if ((key === "7" || code === "Digit7") && event.shiftKey) transaction = toggleBlockMarkdown(view.state, "ordered");
    else if ((key === "8" || code === "Digit8") && event.shiftKey) transaction = toggleBlockMarkdown(view.state, "bullet");
    else if ((key === "9" || code === "Digit9") && event.shiftKey) transaction = toggleBlockMarkdown(view.state, "quote");
  } else if (!modifier && !event.altKey && event.key === "Enter") {
    transaction = continueMarkdownList(view.state);
  } else if (!modifier && !event.altKey && event.key === "Tab") {
    transaction = indentMarkdownList(view.state, event.shiftKey);
  }

  if (!transaction) return false;
  event.preventDefault();
  return dispatchTransaction(view, transaction);
}
