import { tagHighlighter, tags as t } from "@lezer/highlight";

/**
 * Lezer 语法元素到高亮类名的映射：CodeMirror（文件阅读器、配置编辑器）与 diff 共用这一份。
 *
 * 这里只给类名，颜色与字重在 globals.css 的 `.syntax-*` 规则里、取 `--syntax-*` 变量，与 Markdown 代码块的
 * Shiki 主题同源；外观切换由 `.dark` 类生效。用固定类名而不用 CodeMirror 的 `HighlightStyle`：后者的类名是
 * 运行期生成、要先把样式模块挂进文档，diff 这类不经编辑器的渲染面用不上。
 * 标识符、属性、参数、命名空间与运算符不登记，按正文色显示。
 */
export const syntaxHighlighter = tagHighlighter([
  { tag: t.keyword, class: "syntax-keyword" },
  { tag: [t.string, t.special(t.string), t.character, t.docString], class: "syntax-string" },
  { tag: [t.regexp, t.escape], class: "syntax-string-expression" },
  { tag: [t.comment, t.meta, t.processingInstruction, t.quote], class: "syntax-comment" },
  {
    tag: [t.number, t.bool, t.null, t.atom, t.unit, t.self, t.constant(t.variableName), t.standard(t.variableName)],
    class: "syntax-constant",
  },
  {
    tag: [
      t.function(t.variableName),
      t.function(t.propertyName),
      t.function(t.definition(t.variableName)),
      t.macroName,
      t.className,
      t.typeName,
      t.attributeName,
    ],
    class: "syntax-function",
  },
  { tag: t.tagName, class: "syntax-tag" },
  { tag: t.heading, class: "syntax-heading" },
  { tag: t.strong, class: "syntax-strong" },
  { tag: t.emphasis, class: "syntax-emphasis" },
  { tag: t.strikethrough, class: "syntax-strikethrough" },
  { tag: [t.link, t.url], class: "syntax-link" },
  { tag: [t.invalid, t.deleted], class: "syntax-deleted" },
  { tag: t.inserted, class: "syntax-inserted" },
  { tag: t.changed, class: "syntax-changed" },
]);
