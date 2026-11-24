import { describe, expect, it } from "vitest";

import type { TransportToolCallPart } from "@/lib/assistant/contract";
import { CAPSULES_PER_ROW, collectDeliverableBatches, deliverableGroups, deliverableRows, TILES_PER_ROW } from "@/lib/assistant/turn/deliverable-layout";
import { deliveredDirectory, deliveredFile, deliveredUrl, toolCallPart } from "./transport-fixtures";

const present = (id: string, deliverables?: TransportToolCallPart["deliverables"], status: TransportToolCallPart["status"] = "completed") =>
  toolCallPart(id, "present_deliverables", { status, presentation: "quiet", ...(deliverables ? { deliverables } : {}) });

const batches = (...calls: TransportToolCallPart[]) => collectDeliverableBatches(calls.map((part, index) => ({ index: index * 2, part })));
const one = (items: TransportToolCallPart["deliverables"]) => batches(present("p", items))[0];

// 目的：一次交付一张卡，说明与它那批成果对得上号；潜在缺陷：多次交付合成一张后说明对不上，或「再交付一遍」时同一文件出现两次。
describe("collectDeliverableBatches", () => {
  it("每次调用一张卡；同一 key 只留在最后一次交付的那张里", () => {
    const a = deliveredFile("a.md"), b = deliveredDirectory("dist"), url = deliveredUrl("http://localhost:5173/");
    const again = { ...a, caption: "改过了" };
    const cards = batches(present("p1", [a, b]), present("p2", [url, again]));
    expect(cards.map((card) => [card.index, card.items.map((item) => item.deliverable)])).toEqual([[0, [b]], [2, [url, again]]]);
    expect(cards[1].items.at(-1)?.toolCallId).toBe("p2");
  });

  // 潜在缺陷：前一张的成果全被后来重交，还剩一张只有说明的空卡；失败的调用画出空卡。
  it("成果全被后来重交的那张与没有记录的调用都不成卡", () => {
    const a = deliveredFile("a.md");
    expect(batches(present("p1", [a]), present("p2", [a])).map((card) => card.part.toolCallId)).toEqual(["p2"]);
    expect(batches(present("p0", undefined, "failed"))).toEqual([]);
  });

  // 潜在缺陷：说明读错地方（参数里的原文），或别的展示数据被当成说明。
  it("说明只取交付卡展示数据的 note", () => {
    const noted = { ...present("p", [deliveredFile("a.md")]), display_data: { kind: "deliverables", note: "报告与原始数据" } };
    expect(batches(noted)[0].note).toBe("报告与原始数据");
    expect(batches({ ...noted, display_data: { kind: "deliverables", note: null } })[0].note).toBeNull();
    expect(batches({ ...noted, display_data: { kind: "next-step", note: "x" } })[0].note).toBeNull();
  });

  it("胶囊按每行固定枚数估算行数", () => {
    const items = Array.from({ length: CAPSULES_PER_ROW + 1 }, (_, i) => deliveredFile(`f${i}.md`));
    expect(deliverableRows(one(items))).toEqual({ tileRows: 0, cardHeight: 0, capsuleRows: 2 });
  });

  // 潜在缺陷：工作区外的图片被排成方块却没有图源；只有一张图时仍是小方块。
  it("工作区内的位图与 SVG 是方块、其余胶囊；只有一张有宽高的图时是单张大图卡", () => {
    const image = deliveredFile("a.png", { kind: "image", width: 800, height: 400 });
    const svg = deliveredFile("b.svg", { kind: "svg" });
    const outside = deliveredFile(null, { absolutePath: "/x/c.png", kind: "image" });
    const doc = deliveredFile("d.md");
    const groups = deliverableGroups(one([doc, image, outside]));
    expect(groups.tiles).toEqual([]);
    expect(groups.card).toEqual({ entry: { deliverable: image, toolCallId: "p" }, size: { width: 360, height: 180 } });
    expect(groups.capsules.map((entry) => entry.deliverable)).toEqual([doc, outside]);
    const two = one([image, svg, doc]);
    expect(deliverableGroups(two).card).toBeNull();
    expect(deliverableRows(two)).toEqual({ tileRows: 1, cardHeight: 0, capsuleRows: 1 });
    const many = Array.from({ length: TILES_PER_ROW + 1 }, (_, i) => deliveredFile(`i${i}.png`, { kind: "image" }));
    expect(deliverableRows(one(many)).tileRows).toBe(2);
  });

  // 潜在缺陷：成卡条件分在布局与渲染两层——估高按大图卡、渲染却是方块（SVG），或首帧方块、读到宽高后换成卡片（高度跳）。
  it("是否成卡只看记录：没有宽高（SVG、读不出的位图）与装进去比方块还窄的竖长图都是方块", () => {
    const single = (deliverable: ReturnType<typeof deliveredFile>) => deliverableGroups(one([deliverable]));
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
