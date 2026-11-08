import { EditorSelection, type EditorState, type TransactionSpec } from "@codemirror/state";
import { EditorView } from "codemirror";

import { isImeComposing } from "@/lib/keyboard/ime";

type InlineMarkdownMarker = "**" | "*" | "~~" | "`";
type BlockMarkdownKind = "bullet" | "ordered" | "quote";

// 块前缀的切换规则：判定、移除与「任一列表行」的并集都由这张表推出。
// 回车续写用 `listContinuationPrefix` 的另一条规则（引用后必须有空白），不由它推出。
const BLOCK_MARKERS: Record<BlockMarkdownKind, string> = {
  bullet: "[-*+]\\s+",
  ordered: "\\d+[.)]\\s+",
  quote: ">\\s?",
};
const BLOCK_PREFIX = Object.fromEntries(
  Object.entries(BLOCK_MARKERS).map(([kind, marker]) => [kind, new RegExp(`^(\\s*)${marker}`)]),
) as Record<BlockMarkdownKind, RegExp>;
const LIST_LINE_PATTERN = new RegExp(`^(\\s*)(?:${Object.values(BLOCK_MARKERS).join("|")})`);

/** 行首空白（缩进）；与正则 `\s` 是同一组空白字符。 */
function leadingWhitespace(text: string): string {
  return text.slice(0, text.length - text.trimStart().length);
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

/** 要写入的块前缀；编号列表用调用方给的序号。 */
function blockPrefix(kind: BlockMarkdownKind, line: string, orderedNumber: number): string {
  const indentation = leadingWhitespace(line);
  if (kind === "ordered") return `${indentation}${orderedNumber}. `;
  if (kind === "quote") return `${indentation}> `;
  return `${indentation}- `;
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
  const shouldRemove = lines.every((line) => BLOCK_PREFIX[kind].test(line.text));
  let orderedNumber = 1;
  const changes = lines.map((line) => {
    if (shouldRemove) {
      return { from: line.from, to: line.to, insert: line.text.replace(BLOCK_PREFIX[kind], "$1") };
    }

    const prefix = blockPrefix(kind, line.text, orderedNumber);
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
      const whitespace = leadingWhitespace(line.text);
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
 * `false` 表示交给 CodeMirror 其余 keymap 处理。
 * 输入法组字期间的按键（包括确认候选词的 Enter）一律交还 CodeMirror，否则会误续列表前缀。
 */
export function handleMarkdownKeydown(view: EditorView, event: KeyboardEvent): boolean {
  if (isImeComposing(event, view.composing)) return false;

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
  view.dispatch({ ...transaction, userEvent: "input", scrollIntoView: true });
  return true;
}
