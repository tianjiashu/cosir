import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ChangesCard, findLatestChange } from "@/components/assistant-ui/turn/changes-card";
import type { TransportToolCallPart, TransportToolDisplayData } from "@/lib/assistant/contract";
import { aggregateChanges } from "@/lib/assistant/turn-layout";

const PATCH = "diff --git a/src/task.ts b/src/task.ts\n--- a/src/task.ts\n+++ b/src/task.ts\n@@ -1 +1 @@\n-const TICK_MS = 250\n+const tickMs = config.tickMs\n";

function changePart(id: string, changes: Record<string, unknown>[]): { index: number; part: TransportToolCallPart } {
  return {
    index: Number(id),
    part: { type: "tool-call", toolCallId: id, toolName: "apply_patch", args: {}, status: "completed", awaitingUser: false, display_data: { kind: "file-changes", changes } },
  };
}

describe("ChangesCard", () => {
  const calls = [
    changePart("1", [{ path: "src/task.ts", status: "modified", insertions: 21, deletions: 4, patch: PATCH }]),
    changePart("2", [
      { path: "src/config.ts", status: "modified", insertions: 9, deletions: 2 },
      { path: "tests/task.test.ts", status: "added", insertions: 18, deletions: 0 },
    ]),
    changePart("3", [{ path: "old.ts", new_path: "new.ts", status: "moved", insertions: 0, deletions: 0 }]),
  ];
  const card = aggregateChanges(calls);
  const displays: TransportToolDisplayData[] = calls.map((call) => call.part.display_data!);

  it("表头是文件数与总增删，文件行带状态标签与行内 +/−，不画边框", () => {
    const html = renderToStaticMarkup(<ChangesCard card={card} displays={displays} />);
    expect(html).toContain("本轮改动 4 个文件");
    expect(html).toContain("+48");
    expect(html).toContain("−6");
    expect(html).toContain("tests/task.test.ts");
    expect(html).toContain(">新建<");
    expect(html).toContain(">移动<");
    expect(html).toContain("old.ts");
    expect(html).toContain("new.ts");
    expect(html).toContain("bg-surface");
    expect(html).not.toMatch(/class="[^"]*\bborder\b/);
  });

  it("点开文件时取该文件最后一次变更的 diff 载荷", () => {
    const file = card.files.find((item) => item.path === "src/task.ts")!;
    expect(findLatestChange(displays, file)?.patch).toBe(PATCH);
    const missing = card.files.find((item) => item.path === "src/config.ts")!;
    expect(findLatestChange(displays, missing)?.patch).toBeNull();
    expect(findLatestChange([], file)).toBeNull();
  });
});
