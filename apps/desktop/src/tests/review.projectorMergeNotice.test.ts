/**
 * 缺陷验证 #1：projector 合并分支丢失 finished 投影的 notice 字段。
 *
 * 背景：projectTimelineIncrementally 中，tool_call_started 先到、同 callId 的
 * tool_call_finished 后到时走「合并更新既有条目」分支（projector.ts 约 287-311 行）。
 * 该分支显式携带了 resultSummary / result / reason / retryable / listEntries /
 * diffEntries / resultData 等字段，但漏掉了 `notice`（后端剥离的索引降级/陈旧提示，
 * 由 @shared/toolDisplayRules 的 projectToolResult 从 data.codegraph.notice 投影产出）。
 * 合并后条目 notice 恒为 undefined，UI 永不展示降级提示 → 信息丢失。
 *
 * 本文件同时含正向对照用例：证明同一 harness 下其余字段确实被合并分支携带，
 * 即第 1 条断言失败是缺陷所致而非测试写错。
 */
import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  selectVisibleEntries,
  type TimelineToolItem,
} from "@/services/timeline/projector";

const NOTICE_TEXT = "代码索引已降级：结果可能不是最新";

function makeEvent(
  eventId: string,
  sequence: number,
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload,
  } as unknown as RuntimeEvent;
}

function makeCodegraphStarted(): RuntimeEvent {
  return makeEvent("ev-started", 1, "tool_call_started", {
    tool_name: "codegraph_search",
    tool_call_id: "call-1",
    arguments: { query: "foo" },
  });
}

/** finished 的 payload.data 能让 projectCodegraphResult 产出 notice + 1 条符号条目。 */
function makeCodegraphFinished(): RuntimeEvent {
  return makeEvent("ev-finished", 2, "tool_call_finished", {
    tool_name: "codegraph_search",
    tool_call_id: "call-1",
    status: "completed",
    content: "foo 定义于 src/a.ts",
    data: {
      codegraph: {
        notice: NOTICE_TEXT,
        items: [{ name: "foo", filePath: "src/a.ts", lineNumber: 12, kind: "function" }],
      },
    },
  });
}

/** 正常时序投影 started → finished，返回合并后的工具条目。 */
function projectStartedThenFinished(): TimelineToolItem {
  let state = createTimelineProjectorState();
  state = projectTimelineIncrementally(state, [makeCodegraphStarted()]);
  state = projectTimelineIncrementally(state, [makeCodegraphFinished()]);
  const toolEntry = selectVisibleEntries(state).find((e) => e.kind === "tool");
  if (!toolEntry || toolEntry.kind !== "tool") {
    throw new Error("投影结果中未找到工具条目，harness 构造有误");
  }
  return toolEntry.item;
}

describe("projector 合并分支字段完整性", () => {
  // 测试目的：验证 started→finished 正常时序下，合并条目保留 finished 投影的 notice。
  // 可能发现的缺陷：合并分支漏拷贝 notice 字段，导致索引降级提示静默丢失。
  it("started 先到、finished 后到时，合并条目应携带 notice（索引降级提示）", () => {
    const item = projectStartedThenFinished();
    expect(item.status).toBe("completed"); // 前置确认走的正是合并分支
    expect(item.notice).toBe(NOTICE_TEXT);
  });

  // 测试目的：正向对照——证明同一 harness 下合并分支对其余字段确实生效。
  // 可能发现的缺陷：无（此用例应 PASS；若它也失败说明测试 harness 写错，而非业务缺陷）。
  it("对照：合并分支正常携带 resultSummary / listEntries / result / requestSummary", () => {
    const item = projectStartedThenFinished();
    expect(item.status).toBe("completed");
    expect(item.resultSummary).toBe("1 symbol");
    expect(item.listEntries).toHaveLength(1);
    expect(item.listEntries?.[0]?.name).toBe("foo");
    expect(item.result).toBe("foo 定义于 src/a.ts");
    // started 携带的请求摘要在合并后应保留（...existing.item 展开结果）。
    expect(item.requestSummary).toBeDefined();
  });

  // 测试目的：正向对照——失败路径下 reason / retryable / error 经合并分支生效。
  // 可能发现的缺陷：无（此用例应 PASS，进一步坐实 harness 正确性）。
  it("对照：失败 finished 合并后携带 error / reason / retryable", () => {
    let state = createTimelineProjectorState();
    state = projectTimelineIncrementally(state, [
      makeEvent("ev-s2", 1, "tool_call_started", {
        tool_name: "read_file",
        tool_call_id: "call-2",
        arguments: { path: "x.ts" },
      }),
    ]);
    state = projectTimelineIncrementally(state, [
      makeEvent("ev-f2", 2, "tool_call_finished", {
        tool_name: "read_file",
        tool_call_id: "call-2",
        status: "error",
        error: "权限不足",
        reason: "文件无读取权限，请检查 ACL",
        retryable: false,
      }),
    ]);
    const toolEntry = selectVisibleEntries(state).find((e) => e.kind === "tool");
    if (!toolEntry || toolEntry.kind !== "tool") {
      throw new Error("投影结果中未找到工具条目，harness 构造有误");
    }
    expect(toolEntry.item.status).toBe("error");
    expect(toolEntry.item.error).toBe("权限不足");
    expect(toolEntry.item.reason).toBe("文件无读取权限，请检查 ACL");
    expect(toolEntry.item.retryable).toBe(false);
  });
});
