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
} from "../../components/context-observer/message-model";
const row = (
  sequence: number,
  patch: Partial<ContextMessage> = {},
): ContextMessage => ({
  id: sequence,
  task_id: 42,
  run_id: 21,
  sequence,
  include_in_context: true,
  created_at: "2026-10-17T01:00:00Z",
  updated_at: "2026-10-17T01:00:00Z",
  role: "assistant",
  content: "",
  ...patch,
});
describe("上下文观测读取投影", () => {
  it("工具关联限定 Run，缺少结果不推断状态", () => {
    const call = row(2, { tool_calls: [{ id: "same", name: "read_file" }] });
    const result = row(3, { role: "tool", tool_call_id: "same" });
    const unrelated = row(4, {
      role: "tool",
      tool_call_id: "same",
      run_id: 22,
    });
    expect(relatedMessages(call, [call, result, unrelated])).toEqual([result]);
    expect(relatedMessages(result, [call, result, unrelated])).toEqual([call]);
    expect(relatedMessages(result, [result])).toEqual([]);
  });
  it("搜索联合角色与纳入筛选，相同时间按序号稳定排序", () => {
    const rows = [
      row(3, { role: "user", content: "needle" }),
      row(2, { role: "human", include_in_context: false, content: "needle" }),
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
  it("缓存变化不跨 Run 比较，缺少明细不当作零命中率", () => {
    const usage = (hit: number) => ({
      input_tokens: 100,
      input_token_details: { cache_read: hit },
    });
    expect([
      ...cacheChanges([
        row(1, { usage_metadata: usage(90) }),
        row(2, { run_id: 22, usage_metadata: usage(10) }),
        row(3),
        row(4, { usage_metadata: usage(80) }),
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
