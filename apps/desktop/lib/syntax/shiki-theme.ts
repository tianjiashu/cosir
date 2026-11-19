import { createCssVariablesTheme, type ThemeRegistration } from "shiki";

/**
 * 对话与文件预览里 Markdown 代码块的 Shiki 主题。颜色只引用 globals.css 的 `--syntax-*`（与 CodeMirror 的
 * `syntax-highlighter.ts` 同一份调色板）；Streamdown 要求浅色、深色各给一个主题，变量本身随 `.dark` 切换，两格
 * 用同一个。
 *
 * 两边的语法分类体系不同（这里是 TextMate 作用域，CodeMirror 是 Lezer 标签），映射各写一份，但守同一条原则：
 * 关键字、字符串、注释、常量、函数与类型、标签着色，标识符、属性、运算符与标点用正文色。Shiki 自带的 CSS 变量
 * 主题在几处违背这条原则（运算符、变量、JSON 的键、键值分隔符着色，HTML 标签与字符串同色），在后面追加的规则
 * 里改回；作用域更具体的规则优先，所以 `keyword.operator.new` 这类关键字运算符仍是关键字色。
 * 两边是否一致由 `tests/unit/syntax-alignment.test.ts` 按同一组样例比对。
 */
const base = createCssVariablesTheme({ name: "cosir-syntax", variablePrefix: "--syntax-" });
const foreground = "var(--syntax-foreground)";

export const syntaxTheme: ThemeRegistration = {
  ...base,
  tokenColors: [
    ...(base.tokenColors ?? []),
    // 作用域写到与 Shiki 原规则一样具体，才压得过它（如 `variable.other.constant`、`support.type.property-name.json`）。
    {
      scope: [
        "keyword.operator", "punctuation.separator.key-value", "variable.parameter",
        "variable.other", "variable.other.constant", "variable.other.object", "variable.other.class", "variable.other.readwrite",
        "support.type.property-name", "support.type.property-name.json",
      ],
      settings: { foreground },
    },
    { scope: ["keyword.operator.new", "keyword.operator.expression", "keyword.operator.word"], settings: { foreground: "var(--syntax-token-keyword)" } },
    { scope: ["entity.name.tag"], settings: { foreground: "var(--syntax-token-tag)" } },
  ],
};
