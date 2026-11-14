import { describe, expect, it } from "vitest";
import type { ObservedMessage, ObservedRun } from "../../lib/api/session-observation";
import {
  cacheChanges,
  contextState,
  filterMessages,
  indexMessages,
  INITIAL_FILTERS,
  messagePreview,
  messageTitle,
  parseId,
  relatedMessages,
} from "../../components/session-observer/message-model";
import {
  mergeTimeline,
  nodeNotes,
  nodeTitle,
  runNodes,
} from "../../components/session-observer/run-nodes";
const row = (
  sequence: number,
  patch: Partial<ObservedMessage> = {},
): ObservedMessage => ({
  id: sequence,
  session_id: 42,
  turn: 1,
  inherited: false,
  sequence,
  include_in_context: true,
  project_to_transport: true,
  withdrawn: false,
  created_at: "2026-10-17T01:00:00Z",
  updated_at: "2026-10-17T01:00:00Z",
  role: "assistant",
  content: "",
  ...patch,
});
const at = (seconds: number) => new Date(Date.UTC(2026, 9, 17, 1, 0, seconds)).toISOString();
const run = (statuses: Array<[number, string, string?]>, patch: Partial<ObservedRun> = {}): ObservedRun => ({
  status: statuses[statuses.length - 1][1],
  end_reason: null,
  error_code: null,
  usage: null,
  started_at: at(statuses[0][0]),
  updated_at: at(statuses[statuses.length - 1][0]),
  changes: statuses.map(([seconds, status, endReason], index) => ({
    at: at(seconds),
    status,
    previous_status: index ? statuses[index - 1][1] : null,
    previous_at: index ? at(statuses[index - 1][0]) : null,
    end_reason: endReason ?? null,
    latest: index === statuses.length - 1,
  })),
  ...patch,
});
describe("会话观测读取投影", () => {
  it("user 行按来源种类标注和筛选，工具行带工具名", () => {
    const rows = [
      row(1, { role: "user", kind: "user_input" }),
      row(2, { role: "user", kind: "notice" }),
      row(3, { role: "tool", tool_call_id: "c1", tool_name: "ask_user" }),
      row(4, { tool_calls: [{ id: "c1", name: "ask_user" }] }),
      row(5, { role: "user" }),
    ];
    expect(rows.map(messageTitle)).toEqual([
      "用户输入",
      "系统通知",
      "工具结果 · ask_user",
      "模型回复 · ask_user",
      "user",
    ]);
    expect(filterMessages(indexMessages(rows), { ...INITIAL_FILTERS, kind: "notice" }).map(item => item.sequence)).toEqual([2]);
    expect(filterMessages(indexMessages(rows), { ...INITIAL_FILTERS, search: "ask_user" }).map(item => item.sequence)).toEqual([3, 4]);
  });
  it("没进上下文的行给出原因，摘要去掉来源标签", () => {
    expect(contextState(row(1))).toEqual({ included: true, label: "进了上下文" });
    expect(contextState(row(2, { include_in_context: false })).label).toBe("没进上下文 · 未采纳");
    expect(contextState(row(3, { include_in_context: false, withdrawn: true })).label).toBe("没进上下文 · 已撤回");
    expect(messagePreview(row(4, {
      role: "user",
      kind: "user_input",
      content: '<user_input time="2026-11-14T15:00:37+08:00" hint="Follow the rules.">再试试</user_input>',
    }))).toBe("再试试");
    // 模型自己写的标签不是来源包装，原样保留。
    expect(messagePreview(row(5, { content: "<b>粗体</b>" }))).toBe("<b>粗体</b>");
  });
  it("运行节点按时刻插在消息行之间，按回合裁剪后仍能算出间隔", () => {
    const rows = [
      row(1, { created_at: at(1) }),
      row(2, { created_at: at(2) }),
      row(3, { created_at: at(11) }),
    ];
    const nodes = runNodes([
      run([[0, "running"], [3, "suspended"], [10, "running"], [20, "completed", "stop"]], {
        usage: { total_tokens: 46261 },
      }),
    ]);
    const shape = (ascending: boolean) =>
      mergeTimeline(ascending ? rows : [...rows].reverse(), nodes, ascending).map((item) =>
        item.type === "node" ? nodeTitle(item.node.change) : `#${item.message.sequence}`,
      );
    expect(shape(true)).toEqual(["运行开始", "#1", "#2", "挂起", "被唤醒", "#3", "运行完成"]);
    expect(shape(false)).toEqual([...shape(true)].reverse());
    expect(nodes.map(nodeNotes)).toEqual([
      [],
      ["运行了 3 秒"],
      ["挂起了 7 秒"],
      ["运行了 10 秒", "stop", "整个运行 46,261 tokens"],
    ]);
    const stopped = runNodes([run([[0, "running"], [3600, "cancelled", "user_cancelled"], [3700, "running"]])]);
    expect(stopped.map((node) => nodeTitle(node.change))).toEqual(["运行开始", "已停止", "恢复运行"]);
    // 被恢复过的运行，「已停止」不再是最后一次迁移，不挂结局。
    expect(nodeNotes(stopped[1])).toEqual(["运行了 1 小时 0 分", "用户停止"]);
    expect(nodeNotes(stopped[2])).toEqual(["停了 1 分 40 秒"]);
  });
  it("同一次调用的记录按调用 id 关联，可跨回合，缺少结果不推断状态", () => {
    const call = row(2, { tool_calls: [{ id: "c1", name: "read_file" }] });
    const result = row(3, { role: "tool", tool_call_id: "c1" });
    const later = row(5, { role: "tool", tool_call_id: "c1", turn: 2 });
    const unrelated = row(4, { role: "tool", tool_call_id: "c2" });
    expect(relatedMessages(call, [call, result, unrelated, later])).toEqual([result, later]);
    // 同一次调用的另一条结果也算关联记录。
    expect(relatedMessages(later, [call, result, unrelated, later])).toEqual([call, result]);
    expect(relatedMessages(result, [result])).toEqual([]);
  });
  it("回答调用的行与点名调用的系统通知也关联到那次调用", () => {
    const call = row(2, { tool_calls: [{ id: "c1", name: "ask_user" }] });
    const placeholder = row(3, { role: "tool", tool_call_id: "c1" });
    const decision = row(4, { role: "user", kind: "decision", tool_call_id: "c1" });
    const notice = row(5, { role: "user", kind: "notice", content: "Your question (tool_call_id=c1) was not answered." });
    const other = row(6, { role: "user", kind: "notice", content: "环境变化" });
    const all = [call, placeholder, decision, notice, other];
    expect(relatedMessages(placeholder, all)).toEqual([call, decision, notice]);
    expect(relatedMessages(notice, all)).toEqual([call, placeholder, decision]);
    expect(relatedMessages(other, all)).toEqual([]);
  });
  it("搜索联合种类与纳入筛选，相同时间按序号稳定排序", () => {
    const rows = [
      row(3, { role: "user", kind: "user_input", content: "needle" }),
      row(2, { role: "user", kind: "user_input", include_in_context: false, content: "needle" }),
      row(1, { role: "user", kind: "user_input", content: "needle" }),
    ];
    expect(
      filterMessages(indexMessages(rows), {
        ...INITIAL_FILTERS,
        kind: "user_input",
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
