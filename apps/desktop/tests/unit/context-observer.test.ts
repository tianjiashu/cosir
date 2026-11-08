import { describe, expect, it } from "vitest";
import type { ContextMessage } from "../../lib/api/context-messages";
import {
  cacheChanges,
  filterMessages,
  indexMessages,
  INITIAL_FILTERS,
  messagePreview,
  parseId,
  relatedMessages,
  roleLabel,
} from "../../components/context-observer/message-model";
const row = (
  sequence: number,
  patch: Partial<ContextMessage> = {},
): ContextMessage => ({
  id: sequence,
  task_id: 42,
  turn: 1,
  inherited: false,
  sequence,
  include_in_context: true,
  created_at: "2026-10-17T01:00:00Z",
  updated_at: "2026-10-17T01:00:00Z",
  role: "assistant",
  content: "",
  ...patch,
});
describe("上下文观测读取投影", () => {
  it("旧角色不再作为 user 的别名", () => {
    const rows = [row(1, { role: "user" }), row(2, { role: "human" })];
    expect(roleLabel("human")).toBe("human");
    expect(filterMessages(indexMessages(rows), { ...INITIAL_FILTERS, role: "user" }).map(item => item.sequence)).toEqual([1]);
  });
  it("工具结果按调用 id 关联，可跨回合，缺少结果不推断状态", () => {
    const call = row(2, { tool_calls: [{ id: "c1", name: "read_file" }] });
    const result = row(3, { role: "tool", tool_call_id: "c1" });
    const later = row(5, { role: "tool", tool_call_id: "c1", turn: 2 });
    const unrelated = row(4, { role: "tool", tool_call_id: "c2" });
    expect(relatedMessages(call, [call, result, unrelated, later])).toEqual([result, later]);
    expect(relatedMessages(later, [call, result, unrelated, later])).toEqual([call]);
    expect(relatedMessages(result, [result])).toEqual([]);
  });
  it("搜索联合角色与纳入筛选，相同时间按序号稳定排序", () => {
    const rows = [
      row(3, { role: "user", content: "needle" }),
      row(2, { role: "user", include_in_context: false, content: "needle" }),
      row(1, { role: "user", content: "needle" }),
    ];
    expect(
      filterMessages(indexMessages(rows), {
        ...INITIAL_FILTERS,
        role: "user",
        search: "NEEDLE",
        inclusion: "included",
        sort: "updated_at",
      }).map((item) => item.sequence),
    ).toEqual([1, 3]);
    expect(
      filterMessages(indexMessages(rows), {
        ...INITIAL_FILTERS,
        search: "#2",
      }).map((item) => item.sequence),
    ).toEqual([2]);
  });
  it("工具调用没有正文时仍有摘要，多模态载荷不挤入列表", () => {
    expect(
      messagePreview(row(1, { tool_calls: [{ name: "read_file" }] })),
    ).toBe("调用 read_file");
    expect(
      messagePreview(
        row(2, {
          content: [
            { type: "text", text: "查看图片" },
            { type: "image_url", image_url: "large-data" },
          ],
        }),
      ),
    ).toBe("查看图片 [image_url]");
  });
  it("缓存变化只比较本地记录，缺少明细不当作零命中率", () => {
    const usage = (hit: number) => ({
      input_tokens: 100,
      input_token_details: { cache_read: hit },
    });
    expect([
      ...cacheChanges([
        row(1, { usage_metadata: usage(90) }),
        row(2, { inherited: true, usage_metadata: usage(10) }),
        row(3),
        row(4, { turn: 2, usage_metadata: usage(80) }),
        row(5, { inherited: true, usage_metadata: usage(10) }),
      ]),
    ]).toEqual([[4, -10]]);
  });
  it("查询边界拒绝会被 JS 舍入的 ID", () => {
    expect(parseId("9007199254740993")).toBeUndefined();
    expect(parseId("-1")).toBeUndefined();
    expect(parseId("1.2")).toBeUndefined();
    expect(parseId("42")).toBe(42);
  });
});
