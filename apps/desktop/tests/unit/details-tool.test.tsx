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
        data,
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
});
