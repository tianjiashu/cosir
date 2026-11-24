import { describe, expect, it } from "vitest";

import type { TransportDeliverable } from "@/lib/assistant/contract";
import { deliverableWatchKey } from "@/components/deliverables/use-session-deliverable-watch";
import { deliveredDirectory, deliveredFile, transportState } from "./transport-fixtures";

// 目的：监听的集合与侧栏「产出」同源（顶层会话清单），且只在清单变化时变；潜在缺陷：只看已加载消息，窗口外的
// 早期交付物在侧栏显示过期状态；或流式增量每帧换快照，键跟着变，监听反复重连并整体失效文件查询。
describe("deliverableWatchKey", () => {
  it("取顶层清单里工作区内的文件，排序去重，不看消息窗口", () => {
    const deliverables: TransportDeliverable[] = [
      deliveredFile("b.md"),
      deliveredDirectory("dist"),
      deliveredFile(null, { absolutePath: "/x/y.md" }),
      deliveredFile("a.png", { kind: "image" }),
    ];
    expect(JSON.parse(deliverableWatchKey(transportState({ messages: [], deliverables })))).toEqual(["a.png", "b.md"]);
  });

  it("清单不变时换快照也是同一个键，清单变了才变", () => {
    const deliverables = [deliveredFile("a.md")];
    const first = deliverableWatchKey(transportState({ deliverables }));
    expect(deliverableWatchKey(transportState({ title: "流式中", deliverables }))).toBe(first);
    expect(deliverableWatchKey(transportState({ deliverables: [deliveredFile("a.md"), deliveredFile("b.md")] }))).not.toBe(first);
  });
});
