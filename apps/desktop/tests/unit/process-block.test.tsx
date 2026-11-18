import type { TransportToolCallPart } from "@/lib/assistant/contract";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
const overrides = vi.hoisted(() => new Map<string, boolean>());
vi.mock("@/lib/assistant/turn/disclosure-store", async (original) => ({
  ...await original<typeof import("@/lib/assistant/turn/disclosure-store")>(),
  useDisclosure: (_sessionId: number, key: string, defaultOpen: boolean) => [overrides.get(key) ?? defaultOpen, () => undefined],
}));
import { ProcessBlock } from "@/components/conversation/turn/process-block";
import { projectTurn } from "@/lib/assistant/turn/turn-layout";
import { toolCallPart } from "./transport-fixtures";
import type { TransportMessagePart } from "@/lib/assistant/contract";
const call = (id: string, status: TransportToolCallPart["status"] = "completed") => toolCallPart(id, "read_file", { status });
function render(status: TransportToolCallPart["status"] = "completed") {
  const group = projectTurn([call("a"), call("b", status)], "running").blocks[0];
  if (group.kind !== "activity") throw new Error("Expected activity interval");
  return renderToStaticMarkup(<ProcessBlock sessionId={1} messageId="m" group={group} renderCall={(item, visible) => <span data-call={item.part.toolCallId} data-visible={visible} />} />);
}
beforeEach(() => overrides.clear());
describe("activity group", () => {
  it("folds successful details and keeps an accurate call count", () => {
    const html = render();
    expect(html).toContain("读取文件 2 次");
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('data-call="a" data-visible="false"');
  });
  it("folds running calls as well as completed calls", () => {
    expect(render("running")).toContain('data-call="b" data-visible="false"');
    expect(render("running")).toContain('data-call="a" data-visible="false"');
  });
  it("restores user disclosure and retains error/cancel summaries", () => {
    overrides.set("activity:m:interval:0", true);
    expect(render()).toContain('data-call="a" data-visible="true"');
    expect(render("failed")).toContain("1 个失败");
    expect(render("cancelled")).toContain("1 个取消");
  });
});

// 目的：产出上浮到汇总行；潜在缺陷：缩略图嵌进展开按钮（按钮套按钮）、改动量缺失、超过 3 张全铺开。
describe("activity row outputs", () => {
  const asset = (c: string) => c.repeat(64);
  const parts: TransportMessagePart[] = [
    toolCallPart("e", "edit_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status: "modified", patch: null, insertions: 12, deletions: 3 }] } }),
    toolCallPart("r", "read_image", { status: "completed", display_data: { kind: "image-read", images: ["a", "b", "c", "d", "e"].map((c) => ({ path: `${c}.png`, asset_id: asset(c), width: 1, height: 1 })), failures: [] } }),
  ];
  it("shows the summed diff in the head and the latest three images beside it", () => {
    const group = projectTurn(parts, "settled").blocks[0];
    if (group.kind !== "activity") throw new Error("Expected activity interval");
    const html = renderToStaticMarkup(<ProcessBlock sessionId={1} messageId="m" group={group} renderCall={() => null} />);
    const head = html.slice(html.indexOf('data-slot="activity-head"'), html.indexOf("</button>"));
    expect(head).toContain("+12");
    expect(head).toContain("−3");
    expect(head).not.toContain("image-tile");
    const images = html.slice(html.indexOf('data-slot="activity-images"'));
    expect(images.match(/data-slot="image-tile"/g)).toHaveLength(3);
    expect(images).toContain("+2");
    // 静态渲染没有工作区，方块显示占位图标，替代文本在 aria-label 上。
    expect(images).toContain('aria-label="e.png"');
    expect(images).not.toContain('aria-label="a.png"');
  });
});
