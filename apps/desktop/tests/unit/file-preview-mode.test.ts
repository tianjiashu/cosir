import { describe, expect, it } from "vitest";

import { FILE_KINDS } from "@/lib/api/workspace-files";
import { filePreviewMode, opensLinesInSource } from "@/lib/workspace-files/file-preview-mode";

const REASON = "此格式或编码无法在应用内播放";

describe("filePreviewMode", () => {
  it("没有降级时按类型选预览器，视图默认渲染", () => {
    const surfaces = Object.fromEntries(FILE_KINDS.map((kind) => [kind, filePreviewMode(kind, undefined, null).surface]));
    expect(surfaces).toEqual({
      text: "text", markdown: "text", table: "table", mermaid: "text",
      image: "image", svg: "image", pdf: "pdf", audio: "media", video: "media", binary: "binary",
    });
    expect(filePreviewMode("pdf", undefined, null).view).toBe("rendered");
  });

  // 潜在缺陷：表格的「显示不了」排在文本渲染之后，兜底只能渲染成空白。
  it("渲染视图里显示不了优先于一切渲染器，包括由文本渲染的表格", () => {
    for (const kind of FILE_KINDS.filter((item) => item !== "text"))
      expect(filePreviewMode(kind, "rendered", REASON).surface).toBe("binary");
  });

  it("源码视图一律读文本，纯文本永远是源码", () => {
    for (const kind of FILE_KINDS) expect(filePreviewMode(kind, "source", REASON).surface).toBe("text");
    expect(filePreviewMode("text", "rendered", null)).toEqual({ view: "source", surface: "text", switchable: false });
  });

  // 潜在缺陷：PDF、音视频、位图降级后看过源码，换版本或重开（「显示不了」已失效）就再也切不回渲染。
  it("不是纯文本、当前在源码视图就有切换按钮，不依赖显示不了这个瞬时事实", () => {
    for (const kind of ["pdf", "audio", "video", "image"] as const) {
      expect(filePreviewMode(kind, "source", null).switchable).toBe(true);
      expect(filePreviewMode(kind, "rendered", null).switchable).toBe(false);
      expect(filePreviewMode(kind, "rendered", REASON).switchable).toBe(true);
    }
    for (const kind of ["markdown", "table", "mermaid", "svg", "binary"] as const) expect(filePreviewMode(kind, "rendered", null).switchable).toBe(true);
  });

  it("带行号打开只对天生两种视图都有的类型切到源码", () => {
    expect(FILE_KINDS.filter(opensLinesInSource)).toEqual(["markdown", "table", "mermaid", "svg", "binary"]);
  });
});
