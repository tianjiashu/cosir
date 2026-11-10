import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { OmittedOutputNotice } from "@/components/conversation/tools/terminal-output/omitted-notice";

const links = { workspaceRoot: "/repo", open: vi.fn() };

describe("OmittedOutputNotice", () => {
  it("没有省略行时不渲染", () => {
    expect(renderToStaticMarkup(<OmittedOutputNotice omittedLines={0} links={links} />)).toBe("");
    expect(renderToStaticMarkup(<OmittedOutputNotice links={links} />)).toBe("");
  });

  it("有省略行时写明省略数量", () => {
    const html = renderToStaticMarkup(<OmittedOutputNotice omittedLines={12} links={links} />);
    expect(html).toContain("前面省略 12 行");
    expect(html).not.toContain("打开完整日志");
  });

  it("有工作区日志路径时给出打开入口", () => {
    const html = renderToStaticMarkup(
      <OmittedOutputNotice omittedLines={3} logPath=".cosir/tool-artifacts/x.txt" links={links} />,
    );
    expect(html).toContain("打开完整日志");
    expect(html).toContain("<button");
  });

  it("日志路径无法在工作区内解析时只显示文字", () => {
    const html = renderToStaticMarkup(
      <OmittedOutputNotice omittedLines={3} logPath="/outside/x.txt" links={{ workspaceRoot: "/repo", open: vi.fn() }} />,
    );
    expect(html).toContain("前面省略 3 行");
    expect(html).not.toContain("打开完整日志");
  });
});
