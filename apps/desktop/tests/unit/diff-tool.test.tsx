import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DiffTool, parseFileDiff } from "@/components/assistant-ui/tools/diff-tool";

function renderDiff(
  status: "pending" | "running" | "completed",
  display_data: Record<string, unknown> | null,
  options: { verb?: string; defaultOpen?: boolean } = {},
): string {
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
        presentation: {
          verb: options.verb ?? "写入文件",
          icon: "git-compare",
          surface: "standalone",
          expandable: true,
          expand_layout: "diff",
          default_open: options.defaultOpen,
        },
        display_data,
      }}
    />,
  );
}

describe("DiffTool", () => {
  it("parses the backend Git-style patch when the diff is opened", () => {
    const parsed = parseFileDiff({
      path: "src/app.ts",
      patch: [
        "diff --git a/src/app.ts b/src/app.ts",
        "--- a/src/app.ts",
        "+++ b/src/app.ts",
        "@@ -1,1 +1,1 @@",
        "-old",
        "+new",
      ].join("\n"),
    });

    expect(parsed.kind).toBe("ready");
    if (parsed.kind === "ready") {
      expect(parsed.file.hunks).toHaveLength(1);
      expect(parsed.file.hunks[0].changes).toHaveLength(2);
    }
  });

  it("degrades locally for missing, truncated, and malformed patches", () => {
    expect(parseFileDiff({ path: "a.ts", patch: null }).kind).toBe("unavailable");
    expect(parseFileDiff({ path: "a.ts", patch: "too large", truncated: true })).toEqual({
      kind: "unavailable",
      reason: "truncated",
    });
    expect(parseFileDiff({ path: "a.ts", patch: "diff --git a/a.ts b/a.ts\n---" })).toEqual({
      kind: "unavailable",
      reason: "invalid",
    });
  });

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
      changes: [{
        path: "src/app.ts",
        patch: "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
        insertions: 1,
        deletions: 1,
      }],
      diff_stats: { total_files: 1, total_insertions: 1, total_deletions: 1 },
    });

    expect(html).toContain("1 个文件");
    expect(html).toContain("+1");
    expect(html).toContain("−1");
    expect(html).toContain("disclosure-row-chevron");
  });

  it("renders a move as source and destination without fake zero-line stats", () => {
    const html = renderDiff("completed", {
      kind: "file-changes",
      changes: [{
        path: "src/old.ts",
        new_path: "src/new.ts",
        status: "moved",
        patch: "diff --git a/src/old.ts b/src/new.ts\nsimilarity index 100%\nrename from src/old.ts\nrename to src/new.ts",
        insertions: 0,
        deletions: 0,
      }],
      diff_stats: { total_files: 1, total_insertions: 0, total_deletions: 0 },
    }, { verb: "移动文件", defaultOpen: true });

    expect(html).toContain("移动文件");
    expect(html).toContain("已移动");
    expect(html).toContain("src/old.ts");
    expect(html).toContain("src/new.ts");
    expect(html).not.toContain("+0");
    expect(html).not.toContain("−0");
    expect(html).not.toContain("没有文本差异");
  });

  it("labels a deleted file and renders no diff even when a legacy patch is present", () => {
    const html = renderDiff("completed", {
      kind: "file-changes",
      changes: [{
        path: "obsolete.ts",
        status: "deleted",
        patch: "diff --git a/obsolete.ts b/obsolete.ts\n--- a/obsolete.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-secret removed line",
        insertions: 0,
        deletions: 1,
      }],
      diff_stats: { total_files: 1, total_insertions: 0, total_deletions: 1 },
    }, { verb: "删除文件", defaultOpen: true });

    expect(html).toContain("删除文件");
    expect(html).toContain("已删除");
    expect(html).toContain("obsolete.ts");
    expect(html).not.toContain("secret removed line");
    expect(html).not.toContain("−1");
    expect(html).not.toContain("没有文本差异");
    expect(html).not.toContain("Diff 解析失败");
  });

  it("renders the current delete payload as target plus status without statistics", () => {
    const html = renderDiff("completed", {
      kind: "file-changes",
      changes: [{
        path: "logs/old.log",
        new_path: null,
        status: "deleted",
        patch: null,
        insertions: 0,
        deletions: 0,
      }],
      diff_stats: { total_files: 1, total_insertions: 0, total_deletions: 0 },
    }, { verb: "删除文件", defaultOpen: true });

    expect(html).toContain("删除文件");
    expect(html).toContain("1 个文件");
    expect(html).toContain("已删除：");
    expect(html).toContain("logs/old.log");
    expect(html).not.toContain("−");
    expect(html).not.toContain("+0");
    expect(html).not.toContain("Diff 数据不可用");
  });
});
