import { describe, expect, it } from "vitest";

import type { TransportToolCallPart } from "@/lib/assistant/contract";
import { CAPSULES_PER_ROW, collectDeliverables, deliverableGroups, deliverableRows, TILES_PER_ROW, workspaceDeliverablePaths } from "@/lib/assistant/turn/deliverable-layout";
import type { TransportMessage } from "@/lib/assistant/contract";
import { deliveredDirectory, deliveredFile, deliveredUrl, toolCallPart } from "./transport-fixtures";

const present = (id: string, deliverables?: TransportToolCallPart["deliverables"], status: TransportToolCallPart["status"] = "completed") =>
  toolCallPart(id, "present_deliverables", { status, presentation: "quiet", ...(deliverables ? { deliverables } : {}) });

// 目的：一个回合里的交付物是一组；潜在缺陷：模型分两次交付时出现两张卡，或「再交付一遍」时同一文件出现两枚胶囊。
describe("collectDeliverables", () => {
  it("同一回合多次调用合成一组，同一 key 只留最后一次、位置也取最后一次", () => {
    const a = deliveredFile("a.md"), b = deliveredDirectory("dist"), url = deliveredUrl("http://localhost:5173/");
    const again = { ...a, caption: "改过了" };
    const card = collectDeliverables([{ index: 1, part: present("p1", [a, b]) }, { index: 4, part: present("p2", [url, again]) }])!;
    expect(card.items.map((item) => item.deliverable)).toEqual([b, url, again]);
    expect(card.items.at(-1)?.toolCallId).toBe("p2");
    expect(card.index).toBe(1);
    expect(card.callIndexes).toEqual([1, 4]);
  });

  // 潜在缺陷：失败的调用把卡片挪到它的位置，或让本来没有交付物的回合出现空卡。
  it("没有记录的调用（失败、取消、运行中）不占位", () => {
    expect(collectDeliverables([{ index: 0, part: present("p0", undefined, "failed") }])).toBeNull();
    const card = collectDeliverables([{ index: 0, part: present("p0", undefined, "failed") }, { index: 2, part: present("p1", [deliveredFile("a.md")]) }])!;
    expect(card.index).toBe(2);
    expect(card.callIndexes).toEqual([2]);
  });

  it("胶囊按每行固定枚数估算行数", () => {
    const items = Array.from({ length: CAPSULES_PER_ROW + 1 }, (_, i) => deliveredFile(`f${i}.md`));
    expect(deliverableRows(collectDeliverables([{ index: 0, part: present("p", items) }])!)).toEqual({ tileRows: 0, cardHeight: 0, capsuleRows: 2 });
  });

  // 潜在缺陷：工作区外的图片被排成方块却没有图源；只有一张图时仍是小方块。
  it("工作区内的位图与 SVG 是方块、其余胶囊；只有一张有宽高的图时是单张大图卡", () => {
    const image = deliveredFile("a.png", { kind: "image", width: 800, height: 400 });
    const svg = deliveredFile("b.svg", { kind: "svg" });
    const outside = deliveredFile(null, { absolutePath: "/x/c.png", kind: "image" });
    const doc = deliveredFile("d.md");
    const groups = deliverableGroups(collectDeliverables([{ index: 0, part: present("p", [doc, image, outside]) }])!);
    expect(groups.tiles).toEqual([]);
    expect(groups.card).toEqual({ entry: { deliverable: image, toolCallId: "p" }, size: { width: 360, height: 180 } });
    expect(groups.capsules.map((entry) => entry.deliverable)).toEqual([doc, outside]);
    const two = collectDeliverables([{ index: 0, part: present("p", [image, svg, doc]) }])!;
    expect(deliverableGroups(two).card).toBeNull();
    expect(deliverableRows(two)).toEqual({ tileRows: 1, cardHeight: 0, capsuleRows: 1 });
    const many = Array.from({ length: TILES_PER_ROW + 1 }, (_, i) => deliveredFile(`i${i}.png`, { kind: "image" }));
    expect(deliverableRows(collectDeliverables([{ index: 0, part: present("p", many) }])!).tileRows).toBe(2);
  });

  // 潜在缺陷：成卡条件分在布局与渲染两层——估高按大图卡、渲染却是方块（SVG），或首帧方块、读到宽高后换成卡片（高度跳）。
  it("是否成卡只看记录：没有宽高（SVG、读不出的位图）与装进去比方块还窄的竖长图都是方块", () => {
    const single = (deliverable: ReturnType<typeof deliveredFile>) => deliverableGroups(collectDeliverables([{ index: 0, part: present("p", [deliverable]) }])!);
    for (const deliverable of [
      deliveredFile("a.svg", { kind: "svg" }),
      deliveredFile("b.png", { kind: "image" }),
      deliveredFile("long.png", { kind: "image", width: 300, height: 1200 }),
    ]) {
      expect(single(deliverable).card).toBeNull();
      expect(single(deliverable).tiles.map((entry) => entry.deliverable)).toEqual([deliverable]);
    }
    // 小图不放大。
    expect(single(deliveredFile("icon.png", { kind: "image", width: 200, height: 100 })).card?.size).toEqual({ width: 200, height: 100 });
  });
});

// 目的：监听只登记画面上可能出现的卡片；潜在缺陷：目录、工作区外条目也被登记，或同一文件登记两次让集合抖动。
describe("workspaceDeliverablePaths", () => {
  it("只取已加载消息里工作区内的文件，排序去重", () => {
    const message = (parts: TransportMessage["parts"]) => ({ id: "m", role: "assistant", parts, turn: 1, inherited: false, outcome: null }) as TransportMessage;
    const messages = [
      message([present("p1", [deliveredFile("b.md"), deliveredDirectory("dist"), deliveredFile(null, { absolutePath: "/x/y.md" })])]),
      message([present("p2", [deliveredFile("a.png", { kind: "image" }), deliveredFile("b.md")]), { type: "text", text: "完" }]),
    ];
    expect(workspaceDeliverablePaths(messages)).toEqual(["a.png", "b.md"]);
  });
});
