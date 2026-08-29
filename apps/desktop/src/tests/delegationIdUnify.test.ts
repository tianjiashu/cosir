/**
 * delegation 维度 child_turn_id / selectedChildTurnId / parent_turn_id
 * string↔number 类型统一修复的验证（delegationIdUnify）。
 *
 * 不修改任何源码；仅新增测试。
 *
 * 覆盖范围：
 * - readNumberPayload 边界（修复后的 child_turn_id 读取函数，规格复刻）。
 * - 与旧 readStringPayload 对比：旧对 number 的 child_turn_id 返回 null（导致
 *   deriveDelegationStreams 在 `if(!childTurnId) continue` 跳过、委派子流永不创建），
 *   新 readNumberPayload 对 number 返回正确值。
 * - 不变量 4：parent_turn_id 在 projector 导出派生函数
 *   deriveChildDelegationStatus / deriveSiblingDelegations 内 number↔number 比对正确。
 *
 * 关于 readNumberPayload：已从 src/hooks/useDelegationStreams.ts 直接导出并 import 真实函数，
 * 本测试验证的是源码真实实现而非复刻；修复前纯空格串 "  " 被 Number("  ") 误判为 0 的缺陷
 * 现返回 null（见 "  " 空格串用例）。
 */

import { describe, it, expect } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  deriveChildDelegationStatus,
  deriveSiblingDelegations,
} from "@/services/timeline/projector";
// 直接从源码导入真实 readNumberPayload（已导出），避免测试与源码漂移。
// 不再复刻实现：修复线上曾出现「测试复刻与源码不一致导致缺陷未发现」的问题。
import { readNumberPayload } from "@/hooks/useDelegationStreams";

/**
 * 复刻「修复前」的 readStringPayload（旧实现，仅 string 返回、number 一律 null）。
 * 用于演示旧 bug：对 number 的 child_turn_id 返回 null。
 * @see useDelegationStreams.ts:readStringPayload（类型签名已变，此处为旧语义重建）
 */
function readStringPayloadLegacy(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** 构造一条 delegation 相关 RuntimeEvent（最小字段，避免与未导出的内部类型耦合）。 */
function makeDelegationEvent(
  eventId: string,
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
  sequence = 1,
): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: 1,
    turn_id: 99,
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload,
  } as RuntimeEvent;
}

describe("readNumberPayload 边界（delegation child_turn_id 修复后）", () => {
  // 测试目的：验证 number 维度的 child_turn_id 能正确解析；潜在缺陷：非有限/空白值未收敛为 null。
  it("对 number 5 返回 5（child_turn_id 真实维度）", () => {
    expect(readNumberPayload({ child_turn_id: 5 }, "child_turn_id")).toBe(5);
  });

  it("对 string \"5\" 返回 5（协议层偶发 string 也能兼容）", () => {
    expect(readNumberPayload({ child_turn_id: "5" }, "child_turn_id")).toBe(5);
  });

  it("对 0 返回 0（0 是合法有限数，不应被误判为 null）", () => {
    expect(readNumberPayload({ child_turn_id: 0 }, "child_turn_id")).toBe(0);
  });

  it("对负数 -3 返回 -3（负 number 占位/真实负值均合法）", () => {
    expect(readNumberPayload({ child_turn_id: -3 }, "child_turn_id")).toBe(-3);
  });

  it("对 null 返回 null", () => {
    expect(readNumberPayload({ child_turn_id: null }, "child_turn_id")).toBeNull();
  });

  it("对 undefined 返回 null", () => {
    expect(readNumberPayload({}, "child_turn_id")).toBeNull();
  });

  it("对空串 \"\" 返回 null", () => {
    expect(readNumberPayload({ child_turn_id: "" }, "child_turn_id")).toBeNull();
  });

  // 验证修复：readNumberPayload 通过 `typeof value === "string" && value.trim() === ""` 拦截纯空格串，
  // 避免 Number("  ") === 0 被误判为有效 turn id。此用例确认空白串收敛为 null。
  it("对纯空格串 \"  \" 应返回 null（确保空白串不被解析为 0）", () => {
    expect(readNumberPayload({ child_turn_id: "  " }, "child_turn_id")).toBeNull();
  });

  it("对 NaN 返回 null（非有限数）", () => {
    expect(readNumberPayload({ child_turn_id: NaN }, "child_turn_id")).toBeNull();
  });

  it("对 Infinity 返回 null（非有限数）", () => {
    expect(readNumberPayload({ child_turn_id: Infinity }, "child_turn_id")).toBeNull();
  });

  it("对普通对象 {} 返回 null（无法解析为有限数）", () => {
    expect(readNumberPayload({ child_turn_id: {} }, "child_turn_id")).toBeNull();
  });
});

describe("readNumberPayload vs 旧 readStringPayload（修复前 bug 对比）", () => {
  // 测试目的：确认旧实现对 number 返回 null 的 bug 已修复；新实现对 number 返回正确值。
  it("旧 readStringPayload 对 number 5 返回 null（演示旧 bug：委派子流永不创建）", () => {
    expect(readStringPayloadLegacy({ child_turn_id: 5 }, "child_turn_id")).toBeNull();
  });

  it("新 readNumberPayload 对 number 5 返回 5（修复：child_turn_id=5 可被 deriveDelegationStreams 识别）", () => {
    expect(readNumberPayload({ child_turn_id: 5 }, "child_turn_id")).toBe(5);
  });

  it("新 readNumberPayload 对 number child_turn_id=123 不被守卫误判为 null（避免 if(!childTurnId) continue 跳过）", () => {
    // 复刻 useDelegationStreams.ts:121 的守卫条件：childTurnId == null 才会 continue 跳过。
    // 修复后 readNumberPayload 对 123 返回 123，!= null，因此不会跳过，委派子流可创建。
    const childTurnId = readNumberPayload({ delegation_id: "d1", child_turn_id: 123 }, "child_turn_id");
    expect(childTurnId).toBe(123);
    // 代码审查确认（逐字读 useDelegationStreams.ts:110-132）：
    //   const childTurnId = readNumberPayload(payload, "child_turn_id");
    //   if (!delegationId || childTurnId == null) continue;
    // 现 childTurnId === 123（!= null），delegationId 非空时不会 continue，descriptor 被写入 Map。
    // 逻辑路径已通：含 DelegationChildStarted 且 payload.child_turn_id=123 的事件
    // 能产出 childTurnId=123 的 DelegationStreamDescriptor（非导出函数，按 brief 仅验证行为）。
  });
});

describe("不变量 4：projector 派生函数内 parent_turn_id number↔number 比对正确", () => {
  // 测试目的：parent_turn_id 为 number 维度时，投影器反查/分组比对不发生 TS2367 误判，运行正确。
  it("deriveChildDelegationStatus 对 number child_turn_id 正确命中（payload.child_turn_id===childTurnId）", () => {
    const events: RuntimeEvent[] = [
      makeDelegationEvent(
        "c1",
        "delegation_child_started",
        { delegation_id: "d1", parent_turn_id: 7, child_turn_id: 123, child_agent_id: "agent-a", status: "running" },
        1,
      ),
      makeDelegationEvent(
        "c2",
        "delegation_finished",
        { delegation_id: "d1", parent_turn_id: 7, child_turn_id: 123, child_agent_id: "agent-a", status: "completed", summary: "ok" },
        2,
      ),
    ];
    // payload.child_turn_id(number 123) 与入参 number 123 精确比对，应返回最新终态 completed。
    expect(deriveChildDelegationStatus(123, events)).toBe("completed");
    // 不同 number 不应误命中（证明是 number↔number 精确比对，而非恒 false 或恒 true）。
    expect(deriveChildDelegationStatus(999, events)).toBeUndefined();
  });

  it("deriveSiblingDelegations 以 number parent_turn_id 反查并分组，返回含 number childTurnId 的 sibling", () => {
    const events: RuntimeEvent[] = [
      makeDelegationEvent(
        "c1",
        "delegation_child_started",
        { delegation_id: "d1", parent_turn_id: 7, child_turn_id: 123, child_agent_id: "agent-a", status: "running" },
        1,
      ),
      makeDelegationEvent(
        "c2",
        "delegation_child_started",
        { delegation_id: "d2", parent_turn_id: 7, child_turn_id: 124, child_agent_id: "agent-b", status: "running" },
        2,
      ),
      makeDelegationEvent(
        "c3",
        "delegation_child_started",
        { delegation_id: "d3", parent_turn_id: 8, child_turn_id: 125, child_agent_id: "agent-c", status: "running" },
        3,
      ),
    ];
    // 选中 child 123（number）→ 反查 parent_turn_id=7（number）→ 返回同 parent 下全部 sibling。
    const siblings = deriveSiblingDelegations(events, 123);
    const ids = siblings.map((s) => s.childTurnId).sort((a, b) => a - b);
    // parent_turn_id=7 的 sibling：123、124；parent_turn_id=8 的 125 不应混入（证明 number↔number 比对正确）。
    expect(ids).toEqual([123, 124]);
    expect(ids).not.toContain(125);
    // sibling.childTurnId 应为 number 维度（与 SiblingDelegation 接口契约一致）。
    expect(siblings.every((s) => typeof s.childTurnId === "number")).toBe(true);
  });
});
