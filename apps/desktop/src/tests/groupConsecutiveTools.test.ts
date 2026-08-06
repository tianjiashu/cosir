/**
 * `groupConsecutiveTools` 分组边界单测。
 *
 * 覆盖：单条 tool 不套壳、≥2 连续 tool 入组、被非 tool 条目打断的段分离、
 * 以及流式期 groupId 稳定性（组从 2→3 不变、callId 缺失时回退稳定索引）。
 *
 * @module tests/groupConsecutiveTools
 */

import { describe, expect, it } from "vitest";
import type { TurnTimelineEntry } from "@/services/timeline/projector";
import { groupConsecutiveTools } from "@/services/timeline/groupTools";

/** 构造一个最小 tool 条目。callId 缺省为 undefined 以模拟投影层兜底场景。 */
function makeTool(index: number, callId?: string): TurnTimelineEntry {
  return {
    kind: "tool",
    item: {
      eventId: `evt-tool-${index}`,
      toolName: `tool-${index}`,
      status: "running",
      arguments: undefined,
      callId,
      display: undefined,
      requestSummary: `summary-${index}`,
    },
  } as TurnTimelineEntry;
}

/** 构造一个非 tool 条目（assistant）。 */
function makeAssistant(id: string): TurnTimelineEntry {
  return { kind: "assistant", eventId: id, content: "hi" } as TurnTimelineEntry;
}

describe("groupConsecutiveTools", () => {
  it("单条 tool 不套壳，直接复用原 entry 对象", () => {
    const entries = [makeTool(1, "c1")];
    const result = groupConsecutiveTools(entries);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(entries[0]);
    expect(result[0].kind).toBe("tool");
  });

  it("≥2 连续 tool 聚合成一个 toolGroup", () => {
    const entries = [makeTool(1, "c1"), makeTool(2, "c2")];
    const result = groupConsecutiveTools(entries);
    expect(result).toHaveLength(1);
    expect(result[0].kind).toBe("toolGroup");
    if (result[0].kind === "toolGroup") {
      expect(result[0].items).toHaveLength(2);
      expect(result[0].groupId).toBe("grp-c1");
    }
  });

  it("被 assistant 打断的连续段分离为多个组", () => {
    const entries = [
      makeTool(1, "c1"),
      makeTool(2, "c2"),
      makeAssistant("a1"),
      makeTool(3, "c3"),
      makeTool(4, "c4"),
    ];
    const result = groupConsecutiveTools(entries);
    expect(result.map((e) => e.kind)).toEqual([
      "toolGroup",
      "assistant",
      "toolGroup",
    ]);
    if (result[0].kind === "toolGroup") {
      expect(result[0].groupId).toBe("grp-c1");
      expect(result[0].items).toHaveLength(2);
    }
    if (result[2].kind === "toolGroup") {
      expect(result[2].groupId).toBe("grp-c3");
      expect(result[2].items).toHaveLength(2);
    }
  });

  it("流式期组从 2→3 个工具，groupId 保持稳定", () => {
    const two = [makeTool(1, "c1"), makeTool(2, "c2")];
    const three = [makeTool(1, "c1"), makeTool(2, "c2"), makeTool(3, "c3")];
    const g2 = groupConsecutiveTools(two)[0];
    const g3 = groupConsecutiveTools(three)[0];
    expect(g2.kind).toBe("toolGroup");
    expect(g3.kind).toBe("toolGroup");
    if (g2.kind === "toolGroup" && g3.kind === "toolGroup") {
      expect(g3.groupId).toBe(g2.groupId);
    }
  });

  it("callId 缺失时回退稳定索引，流式期 key 仍稳定", () => {
    const two: TurnTimelineEntry[] = [makeTool(1), makeTool(2)];
    const three: TurnTimelineEntry[] = [makeTool(1), makeTool(2), makeTool(3)];
    const g2 = groupConsecutiveTools(two)[0];
    const g3 = groupConsecutiveTools(three)[0];
    expect(g2.kind).toBe("toolGroup");
    expect(g3.kind).toBe("toolGroup");
    if (g2.kind === "toolGroup" && g3.kind === "toolGroup") {
      // 首项在 entries 中索引恒为 0，无论组内工具增长，key 不变。
      expect(g2.groupId).toBe("grp-idx-0");
      expect(g3.groupId).toBe(g2.groupId);
    }
  });

  it("空 entries 返回空数组", () => {
    expect(groupConsecutiveTools([])).toEqual([]);
  });
});
