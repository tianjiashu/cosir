import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { listDetail, pageRange } from "@/components/conversation/tools/call-views/detail-parts";
import { FileDiff, parseFileDiff } from "@/components/conversation/tools/call-views/file-changes-view";
import { readFileChanges } from "@/lib/assistant/tools/file-changes";

// 目的：列表只是其中一页时如实说明范围与总数；潜在缺陷：把「这一页」当「全部」，或第一页且已全部时多一句废话。
describe("list page range", () => {
  it("speaks only when the page is not everything", () => {
    expect(pageRange({ offset: 0, total: 3 }, 3)).toBeNull();
    expect(pageRange({ offset: 0, total: 1000 }, 200)).toBe("第 1–200 条 · 共 1000 条");
    expect(pageRange({ offset: 200, total: 250 }, 50)).toBe("第 201–250 条 · 共 250 条");
    expect(pageRange(undefined, 5)).toBeNull();
  });

  it("puts the range under the bounded list", () => {
    const detail = listDetail({ page: { offset: 0, total: 9 } }, () => [{ v: "a" }, { v: "b" }], "空", (row, i) => <li key={i}>{String(row.v)}</li>);
    const html = renderToStaticMarkup(<>{detail.render()}</>);
    expect(html).toContain('data-slot="detail-budget"');
    expect(html).toContain("第 1–2 条 · 共 9 条");
  });
});

// 目的：超出展示上限没下发的 patch 与「内容没变」是两回事；潜在缺陷：把相同内容写入说成改动过大。
describe("file diff without a patch", () => {
  const change = (patch: unknown) => readFileChanges([{ path: "a.ts", status: "modified", patch, insertions: 9000, deletions: 1 }])[0];
  it("tells omitted apart from unchanged", () => {
    expect(parseFileDiff(change(null))).toEqual({ kind: "unavailable", reason: "omitted" });
    expect(parseFileDiff(change(""))).toEqual({ kind: "empty" });
    expect(renderToStaticMarkup(<FileDiff change={change(null)} />)).toContain("改动过大");
    expect(renderToStaticMarkup(<FileDiff change={change("")} />)).toContain("没有文本差异");
  });
});
