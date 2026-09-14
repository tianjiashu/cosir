import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DetailsTool } from "@/components/assistant-ui/tools/details-tool";

function renderDetails(
  data: Record<string, unknown>,
  options: { toolName?: string; presentation?: Record<string, unknown> } = {},
): string {
  return renderToStaticMarkup(
    <DetailsTool
      type="tool-call"
      toolName={options.toolName ?? "web_search"}
      toolCallId="call-1"
      args={{}}
      argsText="{}"
      status={{ type: "complete", reason: "stop" } as never}
      addResult={() => undefined}
      resume={() => undefined}
      respondToApproval={() => undefined}
      artifact={{
        backendStatus: "completed",
        presentation: options.presentation ?? { expand_layout: "list", default_open: true },
        display_data: data,
      }}
    />,
  );
}

describe("DetailsTool", () => {
  it("does not turn untrusted tool URLs into executable links", () => {
    const html = renderDetails({
      kind: "web-search-results",
      results: [{ title: "unsafe", url: "javascript:alert(1)" }],
    });

    expect(html).toContain("unsafe");
    expect(html).not.toContain("href=\"javascript:");
  });

  it("uses directory-specific empty state wording", () => {
    const html = renderDetails(
      { kind: "directory-list", path: "src", entries: [], total_entries: 0 },
      { toolName: "list_directory" },
    );

    expect(html).toContain("目录为空");
    expect(html).not.toContain("未找到匹配");
  });

  it("does not render a null read range as a line number", () => {
    const html = renderDetails(
      { kind: "read-file-meta", path: "empty.txt", line_range: { start: 1, end: null }, file_size: 0 },
      { toolName: "read_file", presentation: { expand_layout: "none", default_open: true } },
    );

    expect(html).toContain("空文件");
    expect(html).not.toContain("L1-Lnull");
  });

  it("renders read-file metadata in the quiet row", () => {
    const html = renderDetails(
      { kind: "read-file-meta", path: "src/app.ts", line_range: { start: 4, end: 18 }, file_size: 2048 },
      { toolName: "read_file", presentation: { expand_layout: "none", expandable: false } },
    );

    expect(html).toContain("src/app.ts");
    expect(html).toContain("L4-L18");
    expect(html).toContain("2.0 KB");
  });

  it("renders search and directory counts in their rows", () => {
    const searchHtml = renderDetails(
      { kind: "content-search-results", pattern: "TODO", matches: [{ path: "a.ts", line: 3, content: "TODO", is_match: true }], match_count: 3 },
      { toolName: "search_content" },
    );
    const directoryHtml = renderDetails(
      { kind: "directory-list", path: "src", entries: [{ name: "app.ts", type: "file" }], total_entries: 7 },
      { toolName: "list_directory" },
    );

    expect(searchHtml).toContain("TODO · 3 个命中");
    expect(directoryHtml).toContain("src · 7 个条目");
  });

  it("renders web search links and web extract status without extracted body", () => {
    const searchHtml = renderDetails({
      kind: "web-search-results",
      query: "local agents",
      results: [{ title: "Result", url: "https://example.com/result" }],
    });
    const extractHtml = renderDetails({
      kind: "web-extract-urls",
      urls: [{ url: "https://example.com/article" }],
      status_hint: "部分成功",
      content: "must never render",
    }, { toolName: "web_extract", presentation: { expand_layout: "none", expandable: false } });

    expect(searchHtml).toContain("local agents · 1 个结果");
    expect(searchHtml).toContain("https://example.com/result");
    expect(extractHtml).toContain("example.com · 部分成功");
    expect(extractHtml).not.toContain("https://example.com/article");
    expect(extractHtml).not.toContain("must never render");
  });
});
