import { EditorView } from "codemirror";
import {
  syntaxHighlighting,
  defaultHighlightStyle,
} from "@codemirror/language";
import { oneDarkHighlightStyle } from "@codemirror/theme-one-dark";
import { useThemeState } from "@/lib/theme/theme-preference";

const lightSyntax = [
  EditorView.theme({}, { dark: false }),
  syntaxHighlighting(defaultHighlightStyle),
];
const darkSyntax = [
  EditorView.theme({}, { dark: true }),
  syntaxHighlighting(oneDarkHighlightStyle),
];

/** 共享主题只消费既有外观事实；调用方通过 Compartment 更新，保留编辑器选择与滚动。 */
export function useEditorSyntaxTheme() {
  return useThemeState().resolvedTheme === "dark" ? darkSyntax : lightSyntax;
}

/** 配置编辑器与文件阅读器共用应用主题；不包含业务快捷键。 */
export const editorTheme = EditorView.theme({
  "&": {
    backgroundColor: "transparent",
    color: "var(--foreground)",
    height: "100%",
  },
  ".cm-scroller": {
    fontFamily: "var(--app-font-mono)",
    lineHeight: "1.7",
    overflow: "auto",
  },
  ".cm-content": {
    minHeight: "100%",
    padding: "1rem",
  },
  ".cm-gutters": {
    backgroundColor: "transparent",
    border: "0",
    color: "var(--muted-foreground)",
  },
  ".cm-activeLine, .cm-activeLineGutter": {
    backgroundColor: "color-mix(in srgb, var(--muted) 45%, transparent)",
  },
  ".cm-focused": {
    outline: "none",
  },
});
