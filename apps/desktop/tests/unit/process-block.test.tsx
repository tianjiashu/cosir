import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
const overrides = vi.hoisted(() => new Map<string, boolean>());
vi.mock("@/lib/assistant/disclosure-store", async (original) => ({
  ...await original<typeof import("@/lib/assistant/disclosure-store")>(),
  useDisclosure: (_taskId: number, key: string, defaultOpen: boolean) => [overrides.get(key) ?? defaultOpen, () => undefined],
}));
import { ProcessBlock } from "@/components/assistant-ui/turn/process-block";
import { projectTurn } from "@/lib/assistant/turn-layout";
import { toolCallPart } from "./transport-fixtures";
const call = (id: string, status = "completed") => toolCallPart(id, "read_file", { status });
function render(status = "completed") {
  const group = projectTurn([call("a"), call("b", status)], "running").blocks[0];
  if (group.kind !== "activity") throw new Error("Expected activity interval");
  return renderToStaticMarkup(<ProcessBlock taskId={1} messageId="m" group={group} renderCall={(item) => <span data-call={item.part.toolCallId} />} />);
}
beforeEach(() => overrides.clear());
describe("activity group", () => {
  it("folds successful details and keeps an accurate call count", () => {
    const html = render();
    expect(html).toContain("读取文件 2 次");
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('data-call="a"');
  });
  it("keeps active controls mounted even when history is folded", () => {
    expect(render("running")).toContain('data-call="b"');
    expect(render("running")).not.toContain('data-call="a"');
  });
  it("restores user disclosure and retains error/cancel summaries", () => {
    overrides.set("activity:m:a", true);
    expect(render()).toContain('data-call="a"');
    expect(render("failed")).toContain("1 个失败");
    expect(render("cancelled")).toContain("1 个取消");
  });
});
