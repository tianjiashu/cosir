import { EditorView } from "@codemirror/view";
import { EditorState } from "@codemirror/state";

/**
 * 配置编辑器与文件阅读器共用的应用主题；不包含业务快捷键。
 *
 * 字号、行高与界面颜色都取应用的 CSS 变量（语法颜色归着色服务，见 `editor-highlighting.ts`），所以不声明 `dark`：CodeMirror 的基础主题按浅色生效，
 * 它按深浅色区分的几处（选区、光标、面板、浮层、特殊字符）在这里改写成变量。新开基础主题里的部件时，
 * 要在深色下看一眼是否还带着浅色默认值。
 */
export const editorTheme = [
  EditorView.theme({
    "&": {
      backgroundColor: "transparent",
      color: "var(--foreground)",
      fontSize: "var(--text-code)",
      height: "100%",
    },
    ".cm-scroller": {
      fontFamily: "var(--app-font-mono)",
      lineHeight: "var(--text-code--line-height)",
      overflow: "auto",
    },
    // 横向留白放在行上而非内容区：当前行底色才能与行号栏相接，不留断口。
    ".cm-content": {
      minHeight: "100%",
      padding: "0.75rem 0",
    },
    ".cm-line": {
      padding: "0 1rem",
    },
    ".cm-gutters": {
      backgroundColor: "transparent",
      border: "0",
      color: "color-mix(in oklab, var(--muted-foreground) 70%, transparent)",
    },
    ".cm-lineNumbers .cm-gutterElement": {
      paddingLeft: "0.75rem",
    },
    ".cm-activeLine, .cm-activeLineGutter": {
      backgroundColor: "color-mix(in srgb, var(--muted) 45%, transparent)",
    },
    ".cm-activeLineGutter": {
      color: "var(--muted-foreground)",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "var(--foreground)",
    },
    ".cm-selectionBackground": {
      background: "color-mix(in oklab, var(--muted-foreground) 25%, transparent)",
    },
    "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": {
      background: "color-mix(in oklab, var(--info) 25%, transparent)",
    },
    ".cm-selectionMatch": {
      backgroundColor: "color-mix(in oklab, var(--info) 12%, transparent)",
    },
    ".cm-specialChar": {
      color: "var(--destructive)",
    },
    ".cm-foldPlaceholder": {
      backgroundColor: "var(--muted)",
      border: "1px solid var(--border)",
      color: "var(--muted-foreground)",
    },
    ".cm-panels": {
      backgroundColor: "var(--popover)",
      color: "var(--popover-foreground)",
    },
    ".cm-panels-top": {
      borderBottom: "1px solid var(--border)",
    },
    ".cm-panels-bottom": {
      borderTop: "1px solid var(--border)",
    },
    ".cm-textfield": {
      backgroundColor: "var(--background)",
      border: "1px solid var(--input)",
      color: "var(--foreground)",
    },
    ".cm-button": {
      backgroundImage: "none",
      backgroundColor: "var(--secondary)",
      border: "1px solid var(--border)",
      color: "var(--secondary-foreground)",
    },
    // 跳转到行等 CodeMirror 自带的对话面板：与应用的正文字体、控件尺寸一致。
    ".cm-dialog": {
      padding: "0.375rem 2rem 0.375rem 0.75rem",
      fontFamily: "var(--app-font-sans)",
      fontSize: "0.75rem",
    },
    ".cm-dialog label": {
      fontSize: "inherit",
    },
    ".cm-dialog .cm-textfield, .cm-dialog .cm-button": {
      margin: "0 0 0 0.375rem",
      borderRadius: "var(--radius-sm)",
      cornerShape: "var(--corner-shape)",
      padding: "0.125rem 0.5rem",
      fontSize: "inherit",
    },
    ".cm-dialog-close": {
      top: "50%",
      transform: "translateY(-50%)",
      color: "var(--muted-foreground)",
    },
    ".cm-tooltip": {
      backgroundColor: "var(--popover)",
      border: "1px solid var(--border)",
      color: "var(--popover-foreground)",
    },
    "&.cm-focused": {
      outline: "none",
    },
  }),
  // CodeMirror 自带的跳转到行对话框的文案（查找用应用的查找条，不装它的搜索面板）。
  EditorState.phrases.of({
    "Go to line": "跳转到行",
    go: "跳转",
    close: "关闭",
  }),
];
