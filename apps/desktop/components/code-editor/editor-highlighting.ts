import { RangeSetBuilder, type Text } from "@codemirror/state";
import { Decoration, ViewPlugin, type DecorationSet, type EditorView, type ViewUpdate } from "@codemirror/view";
import type { GrammarState, ThemedToken } from "shiki/core";

import type { LanguageHighlighter } from "@/lib/syntax/highlighter";

/** 每隔多少行存一个语法状态：编辑后从改动之前最近的检查点重算。逐行存的话 5 万行要约 18MB。 */
const CHECKPOINT_LINES = 200;
/** 一次让出主线程前最多着色多久；超出就等下一轮计时器，滚动与输入不被卡住。 */
const SLICE_BUDGET_MS = 12;

/**
 * 一篇文档的增量着色结果，不依赖视图（单测直接驱动它）。从头逐行接着算，每行拿上一行末尾的语法状态，结果与整篇
 * 一次着色逐字一致；每算完一行就看预算，所以一次推进最多超出一行的时限。编辑后从改动之前最近的检查点重算。
 * 每行只存「长度、颜色下标」，不留词元对象：五万行要上百 MB。
 */
export class IncrementalHighlighting {
  /** 下标 = 行号 - 1；每行是 [长度, 颜色下标, …]，-1 为无色。 */
  private lines: number[][] = [];
  /** 第 i 个检查点 = 第 i × CHECKPOINT_LINES 行末尾的语法状态（第 0 个是开头，为 undefined）。 */
  private checkpoints: (GrammarState | undefined)[] = [undefined];
  /** 已算到的最后一行末尾的语法状态。 */
  private state: GrammarState | undefined;

  constructor(private readonly highlighter: LanguageHighlighter) {}

  get highlightedLines(): number {
    return this.lines.length;
  }

  line(number: number): readonly number[] {
    return this.lines[number - 1];
  }

  /** 第 `line` 行及之后作废：退回它之前最近的检查点（还没算到那里就不用动）。 */
  invalidateFrom(line: number) {
    const checkpoint = Math.floor((line - 1) / CHECKPOINT_LINES);
    if (this.lines.length <= checkpoint * CHECKPOINT_LINES) return;
    this.lines.length = checkpoint * CHECKPOINT_LINES;
    this.checkpoints.length = checkpoint + 1;
    this.state = this.checkpoints[checkpoint];
  }

  /** 在预算内往 `target` 行接着算（还没到 `target` 时至少一行）。 */
  advance(doc: Text, target: number, budgetMs: number) {
    const started = performance.now();
    const before = this.lines.length;
    while (this.lines.length < target && (this.lines.length === before || performance.now() - started < budgetMs)) {
      const { lines, state } = this.highlighter.chunkTokens(doc.line(this.lines.length + 1).text, this.state);
      this.lines.push(compactLine(lines[0]));
      this.state = state;
      if (this.lines.length % CHECKPOINT_LINES === 0) this.checkpoints[this.lines.length / CHECKPOINT_LINES] = state;
    }
  }
}

/**
 * CodeMirror 的着色，颜色来自全应用唯一的着色服务（`lib/syntax/highlighter.ts`）；CodeMirror 的语言包只管结构。
 *
 * 只算到视口末尾，且尽量不派发事务：构造与 `update` 里先同步算（预算内）、直接换装饰，没算完的才交给后台计时器，
 * 新算出的行进了视口才派发空事务。原因是 CodeMirror 每次派发都会丢掉浏览器里尚未读入的选区与 DOM 改动
 * （`observer.clear()`），全选后紧接着输入时，一次多余的派发会让输入从替换变成追加。所以着色器由调用方备好再
 * 交进来，插件不等异步加载。编辑后视口没算完前沿用旧装饰（随改动平移），颜色不闪。
 */
export function editorHighlighting(highlighter: LanguageHighlighter) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet = Decoration.none;
      private readonly chunks = new IncrementalHighlighting(highlighter);
      /** 当前装饰是不是编辑前的、只随改动平移过的旧装饰（视口还没重算完）。 */
      private stale = false;
      private timer: number | null = null;

      constructor(private readonly view: EditorView) {
        this.chunks.advance(view.state.doc, this.targetLines(), SLICE_BUDGET_MS);
        this.decorations = this.build();
        this.schedule();
      }

      update(update: ViewUpdate) {
        if (!update.docChanged && !update.viewportChanged) return;
        if (update.docChanged) {
          this.chunks.invalidateFrom(firstChangedLine(update));
          this.decorations = this.decorations.map(update.changes);
          this.stale = this.decorations.size > 0;
        }
        this.chunks.advance(update.state.doc, this.targetLines(), SLICE_BUDGET_MS);
        this.refresh();
        this.schedule();
      }

      destroy() {
        if (this.timer !== null) clearTimeout(this.timer);
      }

      /**
       * 换装饰的唯一判断（编辑、滚动与后台三条路径共用）；返回有没有换。视口里还没有着色的行就不换（视口在大文件
       * 末尾时，前面几百段都在视口之上）；有编辑前的旧装饰时，等视口整个算完再一次换上，颜色不闪。
       */
      private refresh(): boolean {
        const { from, to } = this.view.viewport;
        const doc = this.view.state.doc;
        const covered = this.chunks.highlightedLines;
        if (covered < doc.lineAt(from).number || (this.stale && covered < doc.lineAt(to).number)) return false;
        this.stale = false;
        this.decorations = this.build();
        return true;
      }

      /** 还没算到视口末尾时，在下一轮计时器里接着算。 */
      private schedule() {
        if (this.timer !== null || this.chunks.highlightedLines >= this.targetLines()) return;
        this.timer = window.setTimeout(() => {
          this.timer = null;
          this.run();
        }, 0);
      }

      private targetLines() {
        return this.view.state.doc.lineAt(this.view.viewport.to).number;
      }

      /** 后台接着算（计时器只在没算到视口末尾时排上，至少算出一段）；装饰换了才派发空事务，让 CodeMirror 读到它。 */
      private run() {
        this.chunks.advance(this.view.state.doc, this.targetLines(), SLICE_BUDGET_MS);
        if (this.refresh()) this.view.dispatch({});
        this.schedule();
      }

      /** 只给视口里已着色的行建装饰。 */
      private build(): DecorationSet {
        const builder = new RangeSetBuilder<Decoration>();
        const doc = this.view.state.doc;
        const { from, to } = this.view.viewport;
        const last = Math.min(doc.lineAt(to).number, this.chunks.highlightedLines);
        for (let number = doc.lineAt(from).number; number <= last; number++) {
          let offset = doc.line(number).from;
          const tokens = this.chunks.line(number);
          for (let index = 0; index < tokens.length; index += 2) {
            const end = offset + tokens[index];
            if (tokens[index + 1] >= 0) builder.add(offset, end, marks[tokens[index + 1]]);
            offset = end;
          }
        }
        return builder.finish();
      }
    },
    { decorations: (plugin) => plugin.decorations },
  );
}

function firstChangedLine(update: ViewUpdate): number {
  let first = Infinity;
  update.changes.iterChangedRanges((_fromA, _toA, fromB) => {
    first = Math.min(first, fromB);
  });
  return update.state.doc.lineAt(Math.min(first, update.state.doc.length)).number;
}

/** 颜色组合 → 装饰：一篇文件里只有几十种，全应用共用一张表，每行只存下标。 */
const marks: Decoration[] = [];
const markIndex = new Map<string, number>();

export function compactLine(tokens: ThemedToken[]): number[] {
  const compact: number[] = [];
  for (const token of tokens) {
    compact.push(token.content.length, token.htmlStyle?.color ? markOf(token.htmlStyle) : -1);
  }
  return compact;
}

function markOf(style: Record<string, string>): number {
  const css = Object.entries(style).map(([key, value]) => `${key}:${value}`).join(";");
  let index = markIndex.get(css);
  if (index === undefined) {
    index = marks.push(Decoration.mark({ class: "shiki-token", attributes: { style: css } })) - 1;
    markIndex.set(css, index);
  }
  return index;
}
