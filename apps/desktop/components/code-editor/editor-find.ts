import { replaceAll, replaceNext, SearchCursor, SearchQuery, setSearchQuery } from "@codemirror/search";
import { EditorSelection, StateEffect, StateField, type Range } from "@codemirror/state";
import { Decoration, EditorView, type DecorationSet } from "@codemirror/view";

import { MAX_FIND_MATCHES, nextMatchIndex, type FindDirection, type FindResult } from "@/lib/find/next-match";

const setFindDecorations = StateEffect.define<DecorationSet>();
const matchMark = Decoration.mark({ class: "cm-findMatch" });
const currentMark = Decoration.mark({ class: "cm-findMatch-current" });
const lastTerms = new WeakMap<EditorView, string>();

/** 查找命中的装饰层；只由本模块的查找、刷新与替换写入。颜色在 globals.css（与渲染视图的高亮共用）。 */
export const findHighlights = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    for (const effect of transaction.effects) if (effect.is(setFindDecorations)) return effect.value;
    return value.map(transaction.changes);
  },
  provide: (field) => EditorView.decorations.from(field),
});

type Match = { from: number; to: number };

function collectMatches(view: EditorView, term: string): Match[] {
  const { doc } = view.state;
  const matches: Match[] = [];
  const cursor = new SearchCursor(doc, term.toLowerCase(), 0, doc.length, (text) => text.toLowerCase());
  while (matches.length < MAX_FIND_MATCHES && !cursor.next().done) matches.push({ ...cursor.value });
  return matches;
}

function marksOf(matches: Match[], current: number): DecorationSet {
  return Decoration.set(matches.map((match, i): Range<Decoration> => (i === current ? currentMark : matchMark).range(match.from, match.to)));
}

function resultOf(matches: Match[], index: number): FindResult {
  return matches.length >= MAX_FIND_MATCHES ? { index: -1, count: matches.length } : { index, count: matches.length };
}

/**
 * 在编辑器里查找（不区分大小写），标出全部命中、选中并居中当前处。
 *
 * 不用 `@codemirror/search` 自带的面板：查找条是应用统一的 `FindBar`，这里只借它的 `SearchCursor`。
 * 当前处同时成为编辑器选区，因此「添加到对话」会带上它，与常见编辑器一致。空词清除高亮。
 */
export function findInEditor(view: EditorView, term: string, direction: FindDirection): FindResult {
  const fresh = lastTerms.get(view) !== term;
  lastTerms.set(view, term);
  const matches = term ? collectMatches(view, term) : [];
  if (!matches.length) {
    view.dispatch({ effects: setFindDecorations.of(Decoration.none) });
    return { index: -1, count: 0 };
  }
  const index = nextMatchIndex(matches.map((match) => match.from), view.state.selection.main.from, direction, fresh);
  const current = matches[index];
  view.dispatch({
    selection: EditorSelection.single(current.from, current.to),
    effects: [setFindDecorations.of(marksOf(matches, index)), EditorView.scrollIntoView(current.from, { y: "center" })],
  });
  return resultOf(matches, index);
}

/**
 * 文本变了以后按同一个词重算命中与计数，不移动选区、不滚动：编辑时查找条开着，边打字边刷新，光标不被拉走。
 * 当前处取选区所在或之后的第一处（下次「下一个」会停的地方），只标出、不选中。
 */
export function refreshFindInEditor(view: EditorView, term: string): FindResult {
  const matches = term ? collectMatches(view, term) : [];
  const index = matches.length ? nextMatchIndex(matches.map((match) => match.from), view.state.selection.main.from, "next", true) : -1;
  view.dispatch({ effects: setFindDecorations.of(matches.length ? marksOf(matches, index) : Decoration.none) });
  return matches.length ? resultOf(matches, index) : { index: -1, count: 0 };
}

/**
 * 在可编辑编辑器里替换（应用查找条的替换行）：`all` 为假时替换当前命中（选区正好是它时）并停到下一处，为真时
 * 全部替换。匹配规则与 `findInEditor` 相同（字面、不区分大小写）。
 *
 * 替换本身交给 CodeMirror 的 `replaceNext` / `replaceAll`（需要 `search()` 的查询状态，不打开它的面板）：
 * 全部替换是一个事务，一次撤销就能复原；只读状态下它们什么也不做。替换后按新文本重算全部命中，当前处取
 * CodeMirror 停下的位置。
 */
export function replaceInEditor(view: EditorView, term: string, replacement: string, all: boolean): FindResult {
  if (!term) return { index: -1, count: 0 };
  view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: term, replace: replacement, caseSensitive: false, literal: true })) });
  (all ? replaceAll : replaceNext)(view);
  lastTerms.delete(view);
  return findInEditor(view, term, "next");
}
