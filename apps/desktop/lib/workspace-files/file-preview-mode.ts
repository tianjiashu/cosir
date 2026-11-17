import type { FileKind } from "@/lib/api/workspace-files";
import type { ReadingView } from "./workspace-files-store";

/** 文件面板用哪个预览器：`text` 读文本（源码，或 Markdown、Mermaid 的渲染视图），`table` 是表格的渲染视图，其余按类型。 */
export type PreviewSurface = "text" | "table" | "binary" | "image" | "media" | "pdf";

export type FilePreviewMode = {
  /** 实际视图：纯文本只有源码，其余取偏好，没有偏好时是渲染。 */
  view: ReadingView;
  surface: PreviewSurface;
  /** 要不要给「渲染 / 源码」切换按钮。 */
  switchable: boolean;
};

/** 天生既有渲染形态又能看源码的类型；位图、PDF 与音视频只有渲染结果，源码只是显示不了时的出口。 */
const BOTH_VIEWS: ReadonlySet<FileKind> = new Set(["markdown", "table", "mermaid", "svg", "binary"]);

/** 带行号打开时要先切到源码的类型：渲染视图没有行的概念，只有天生两种视图都有的才切。 */
export function opensLinesInSource(kind: FileKind): boolean {
  return BOTH_VIEWS.has(kind);
}

/**
 * 文件面板的预览分发：由类型、视图偏好与当前版本「显示不了」的原因三个事实决定，规则只在这里（`FilePreview` 只按结果
 * 挂组件）。两条容易漏的：
 *
 * - 渲染视图里「显示不了」优先于一切渲染器，包括由文本渲染的表格：否则表格的兜底只能渲染成空白。
 * - 只要不是纯文本、当前又在源码视图，就给切换按钮。视图偏好会跨标签、跨版本留存（关掉标签也不清），而「显示不了」
 *   只是当前版本的瞬时事实；只按它给按钮的话，PDF、音视频、位图降级后看过源码，一换版本或重开就再也切不回渲染。
 */
export function filePreviewMode(kind: FileKind, preferredView: ReadingView | undefined, unviewableReason: string | null): FilePreviewMode {
  const view: ReadingView = kind === "text" ? "source" : (preferredView ?? "rendered");
  const switchable = kind !== "text" && (BOTH_VIEWS.has(kind) || unviewableReason !== null || view === "source");
  return { view, switchable, surface: surfaceOf(kind, view, unviewableReason) };
}

/**
 * 渲染视图下每种类型用哪个预览器：以 `Record` 穷举，新增类型漏写在编译时报错。`binary` 走「看不了的文件」，
 * 显示不了的原因另在 `surfaceOf` 里优先处理。
 */
const RENDERED_SURFACE: Record<FileKind, PreviewSurface> = {
  text: "text",
  markdown: "text",
  mermaid: "text",
  table: "table",
  image: "image",
  svg: "image",
  audio: "media",
  video: "media",
  pdf: "pdf",
  binary: "binary",
};

function surfaceOf(kind: FileKind, view: ReadingView, unviewableReason: string | null): PreviewSurface {
  if (view === "source") return "text";
  if (unviewableReason !== null) return "binary";
  return RENDERED_SURFACE[kind];
}
