// @vitest-environment happy-dom
/**
 * 独立审查：ContextUsageRing / contextUsageStore 的边界与缺陷挖掘。
 *
 * 攻击面（开发方 26 用例未覆盖）：
 *  - activeTaskId 为 null 与为 0 的区分（0 是否合法 task_id）
 *  - context_usage 事件 total_tokens 为 0 / 缺字段 / payload 为 null 时的表现
 *  - resetTask 对不存在的 task 保持引用稳定（AC5 重渲染防线）
 *  - EMPTY_USAGE 必须被 Object.freeze 冻结（防止误改污染所有无数据任务）
 *  - 圆环除零保护（total_tokens=0 时不得 NaN/Infinity）
 *
 * @module tests/indepReview.boundary.storeRing
 */
import { beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { EMPTY_USAGE, useContextUsageStore } from "@/stores/contextUsageStore";
import { useTaskStore } from "@/stores/taskStore";

beforeEach(() => {
  cleanup();
  useContextUsageStore.setState({ usageByTaskId: {} });
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    drafts: {},
  });
});

describe("activeTaskId 为 null 与为 0 的区分", () => {
  // activeTaskId === null 时圆环必须显示 0/0，不读取任何 task（含 task 0）。
  it("null 活跃任务不读取任何 task（含 task 0）", () => {
    useContextUsageStore.getState().setUsage(0, { used_tokens: 90000, total_tokens: 200000 }, "t");
    useTaskStore.setState({ activeTaskId: null });

    render(<ContextUsageRing />);

    // 即便 task 0 有数据，null 活跃态也应回落 0/0。
    expect(document.body.textContent).toContain("0.0%");
    expect(document.body.textContent).not.toContain("90000");
  });

  // 0 是合法 task_id 吗？若 store 用 `activeTaskId ?? ` 之类的写法，0 会被误判为无数据。
  // 这里验证 0 应作为真实 task_id 命中其条目。
  it("0 作活跃任务 id 时能命中 task 0 的占用", () => {
    useContextUsageStore.getState().setUsage(0, { used_tokens: 5000, total_tokens: 128000 }, "t");
    useTaskStore.setState({ activeTaskId: 0 });

    render(<ContextUsageRing />);

    // 5000/128000 ≈ 3.9%：若实现把 0 当 falsy 误判为无数据，会显示 0.0% 而失败。
    expect(document.body.textContent).toContain("3.9%");
    expect(document.body.textContent).not.toContain("0.0%");
  });
});

describe("context_usage 数据质量边界", () => {
  // total_tokens 为 0 时圆环不得出现 NaN / Infinity，必须封顶或回落。
  it("total_tokens 为 0 时圆环不出现 NaN/Infinity", () => {
    useTaskStore.setState({ activeTaskId: 1 });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 5000, total_tokens: 0 }, "t");

    render(<ContextUsageRing />);

    const text = document.body.textContent ?? "";
    expect(text).not.toContain("NaN");
    expect(text).not.toContain("Infinity");
    // 除零保护：totalTokens<=0 时 pct=0 → 0.0%。
    expect(text).toContain("0.0%");
  });

  // 缺字段（used_tokens / total_tokens 缺失）时，store 写入 NaN，圆环不应崩。
  it("payload 缺字段时不致圆环渲染崩溃", () => {
    useTaskStore.setState({ activeTaskId: 1 });
    // 直接通过 setUsage 注入残缺 payload（模拟极端后端返回）。
    (useContextUsageStore.getState().setUsage as unknown as (id: number, p: Record<string, unknown>, u: string) => void)(
      1,
      { used_tokens: undefined as unknown as number, total_tokens: undefined as unknown as number },
      "t",
    );

    expect(() => render(<ContextUsageRing />)).not.toThrow();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("NaN");
  });
});

describe("EMPTY_USAGE 冻结与选择器引用稳定", () => {
  // EMPTY_USAGE 必须被冻结：若未冻结，某处误改会污染所有无数据任务的展示。
  it("EMPTY_USAGE 是不可变对象", () => {
    expect(Object.isFrozen(EMPTY_USAGE)).toBe(true);
    // 尝试修改应无声失败（strict 模式下抛错，但至少不产生副作用）。
    const snapshot = { ...EMPTY_USAGE };
    try {
      (EMPTY_USAGE as { usedTokens: number }).usedTokens = 999;
    } catch {
      // strict 模式可能抛错，忽略。
    }
    expect(EMPTY_USAGE.usedTokens).toBe(snapshot.usedTokens);
  });

  // 无活跃任务时选择器必须返回共享常量（同一引用），避免 AC5 重渲染风暴。
  it("无活跃任务时圆环消费的是共享 EMPTY_USAGE 引用", () => {
    useTaskStore.setState({ activeTaskId: null });
    render(<ContextUsageRing />);
    // 组件已渲染且未抛错；若选择器内联新建对象，多次 store 变更会触发无限重渲染。
    // 此处仅确认组件稳定存在。
    expect(document.body.textContent).toContain("0.0%");
  });
});

describe("resetTask 对不存在的 task 的引用稳定性", () => {
  // AC5：无条目时 resetTask 不创建新对象引用，避免无意义重渲染。
  it("resetTask 不存在的 task 后 usageByTaskId 引用不变", () => {
    const before = useContextUsageStore.getState().usageByTaskId;
    useContextUsageStore.getState().resetTask(12345);
    expect(useContextUsageStore.getState().usageByTaskId).toBe(before);
  });

  // resetTask 存在条目时返回新对象引用（触发订阅者重渲染是预期的）。
  it("resetTask 存在的 task 后返回新引用且条目已删除", () => {
    useContextUsageStore.getState().setUsage(7, { used_tokens: 1, total_tokens: 1 }, "t");
    const before = useContextUsageStore.getState().usageByTaskId;
    useContextUsageStore.getState().resetTask(7);
    const after = useContextUsageStore.getState().usageByTaskId;
    expect(after).not.toBe(before);
    expect(after[7]).toBeUndefined();
  });
});

describe("clearAll 后所有 task 回落 0/0", () => {
  it("clearAll 后活跃 task 的圆环显示 0/0", () => {
    useTaskStore.setState({ activeTaskId: 1 });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 5000, total_tokens: 128000 }, "t");
    useContextUsageStore.getState().clearAll();

    render(<ContextUsageRing />);
    expect(document.body.textContent).toContain("0.0%");
  });
});
