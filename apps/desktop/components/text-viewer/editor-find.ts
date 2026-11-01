import { SearchCursor } from "@codemirror/search";
import { EditorSelection, StateEffect, StateField, type Range } from "@codemirror/state";
import { Decoration, EditorView, type DecorationSet } from "@codemirror/view";

import { MAX_FIND_MATCHES, nextMatchIndex, type FindDirection, type FindResult } from "@/lib/find/next-match";

const setFindDecorations = StateEffect.define<DecorationSet>();
const matchMark = Decoration.mark({ class: "cm-findMatch" });
const currentMark = Decoration.mark({ class: "cm-findMatch-current" });
const lastTerms = new WeakMap<EditorView, string>();

/** 查找命中的装饰层；只由 `findInEditor` 写入。颜色在 globals.css（与渲染视图的高亮共用）。 */
export const findHighlights = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    for (const effect of transaction.effects) if (effect.is(setFindDecorations)) return effect.value;
    return value.map(transaction.changes);
  },
  provide: (field) => EditorView.decorations.from(field),
});

/**
 * 在只读编辑器里查找（不区分大小写），标出全部命中、选中并居中当前处。
 *
 * 不用 `@codemirror/search` 自带的面板：查找条是应用统一的 `FindBar`，这里只借它的 `SearchCursor`。
 * 当前处同时成为编辑器选区，因此「添加到对话」会带上它，与常见编辑器一致。空词清除高亮。
 */
export function findInEditor(view: EditorView, term: string, direction: FindDirection): FindResult {
  const fresh = lastTerms.get(view) !== term;
  lastTerms.set(view, term);
  if (!term) {
    view.dispatch({ effects: setFindDecorations.of(Decoration.none) });
    return { index: -1, count: 0 };
  }
  const { doc, selection } = view.state;
  const matches: { from: number; to: number }[] = [];
  const cursor = new SearchCursor(doc, term.toLowerCase(), 0, doc.length, (text) => text.toLowerCase());
  while (matches.length < MAX_FIND_MATCHES && !cursor.next().done) matches.push({ ...cursor.value });
  if (!matches.length) {
    view.dispatch({ effects: setFindDecorations.of(Decoration.none) });
    return { index: -1, count: 0 };
  }
  const index = nextMatchIndex(matches.map((match) => match.from), selection.main.from, direction, fresh);
  const current = matches[index];
  const marks: Range<Decoration>[] = matches.map((match, i) => (i === index ? currentMark : matchMark).range(match.from, match.to));
  view.dispatch({
    selection: EditorSelection.single(current.from, current.to),
    effects: [setFindDecorations.of(Decoration.set(marks)), EditorView.scrollIntoView(current.from, { y: "center" })],
  });
  return matches.length >= MAX_FIND_MATCHES ? { index: -1, count: matches.length } : { index, count: matches.length };
}
