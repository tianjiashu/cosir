import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
vi.mock("@/lib/assistant/disclosure-store", () => ({ fileKey: (id: string, path: string) => `${id}:${path}`, useDisclosure: () => [true, () => undefined] }));
import { ChangesCard } from "@/components/assistant-ui/turn/changes-card";
import { aggregateChanges } from "@/lib/assistant/turn-layout";
import { toolCallPart } from "./transport-fixtures";

describe("file operation history", () => {
  it("shows repeated operations instead of a synthesized final status/net diff", () => {
    const calls = ["deleted", "added"].map((status, index) => ({ index, part: toolCallPart(`c${index}`, "write_file", {
      status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status, insertions: index ? 8 : 0, deletions: index ? 0 : 4 }] },
    }) }));
    const card = aggregateChanges(calls);
    const html = renderToStaticMarkup(<ChangesCard card={card} displays={calls.map((item) => item.part.display_data!)} taskId={1} messageId="m" />);
    expect(html).toContain("文件操作");
    expect(html).toContain("2 次调用");
    expect(html).toContain("2 次操作");
    expect(html).toContain("删除");
    expect(html).toContain("新建");
    expect(html).not.toContain("本轮改动");
  });
});
