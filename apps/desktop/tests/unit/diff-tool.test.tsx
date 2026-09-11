import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DiffTool } from "@/components/assistant-ui/tools/diff-tool";

function renderDiff(status: "pending" | "running" | "completed", display_data: Record<string, unknown> | null): string {
  return renderToStaticMarkup(
    <DiffTool
      type="tool-call"
      toolName="write_file"
      toolCallId="call-1"
      args={{}}
      argsText="{}"
      status={{ type: status === "completed" ? "complete" : "running" } as never}
      addResult={() => undefined}
      resume={() => undefined}
      respondToApproval={() => undefined}
      artifact={{
        backendStatus: status,
        presentation: { verb: "写入文件", icon: "git-compare", surface: "standalone", expandable: true, expand_layout: "diff" },
        display_data,
      }}
    />,
  );
}

describe("DiffTool", () => {
  it("renders a pending tool shell without fake diff statistics", () => {
    const html = renderDiff("pending", null);

    expect(html).toContain("写入文件");
    expect(html).toContain("准备执行");
    expect(html).not.toContain("0 个文件");
    expect(html).not.toContain("+0");
    expect(html).not.toContain("−0");
    expect(html).not.toContain("disclosure-row-chevron");
  });

  it("renders diff statistics only after display data arrives", () => {
    const html = renderDiff("completed", {
      kind: "file-changes",
      changes: [{ path: "src/app.ts", before: "old", after: "new", insertions: 1, deletions: 1 }],
      diff_stats: { total_files: 1, total_insertions: 1, total_deletions: 1 },
    });

    expect(html).toContain("1 个文件");
    expect(html).toContain("+1");
    expect(html).toContain("−1");
    expect(html).toContain("disclosure-row-chevron");
  });
});
