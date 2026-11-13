import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
vi.mock("@/lib/assistant/turn/disclosure-store", () => ({ fileKey: (id: string, path: string) => `${id}:${path}`, useDisclosure: () => [true, () => undefined] }));
import { ChangesCard } from "@/components/conversation/turn/changes-card";
import { aggregateChanges } from "@/lib/assistant/turn/turn-layout";
import { toolCallPart } from "./transport-fixtures";

describe("file operation history", () => {
  it("renders the correct operation after the display boundary filters an invalid entry", () => {
    const card = aggregateChanges([{ index: 0, part: toolCallPart("move", "move_file", {
      display_data: { kind: "file-changes", changes: [null, { path: "a.ts", status: "moved", new_path: "b.ts" }] },
    }) }]);
    const html = renderToStaticMarkup(<ChangesCard card={card} sessionId={1} messageId="m" />);
    expect(html).toContain("移动");
    expect(html).toContain("a.ts");
    expect(html).not.toContain("Diff 数据不可用");
  });

  it("shows repeated operations instead of a synthesized final status/net diff", () => {
    const calls = ["deleted", "added"].map((status, index) => ({ index, part: toolCallPart(`c${index}`, "write_file", {
      status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status, insertions: index ? 8 : 0, deletions: index ? 0 : 4 }] },
    }) }));
    const card = aggregateChanges(calls);
    const html = renderToStaticMarkup(<ChangesCard card={card} sessionId={1} messageId="m" />);
    expect(html).toContain("文件操作");
    expect(html).toContain("2 次调用");
    expect(html).toContain("2 次操作");
    expect(html).toContain("删除");
    expect(html).toContain("新建");
    expect(html).not.toContain("本轮改动");
  });
});
