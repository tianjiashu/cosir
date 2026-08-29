// @vitest-environment happy-dom
/**
 * ContextUsageRing 按 activeTaskId 选择展示。
 *
 * 守护不变量：圆环只反映当前活跃任务的占用；切换 activeTaskId 时随之切换；无活跃
 * 任务或该任务无占用数据时回落 0/0，**绝不回退到「全局最近一次」或任何其它任务的
 * 占用**。
 *
 * 可证伪性：旧实现直接订阅全局 ``usedTokens`` / ``totalTokens`` 且完全不读
 * ``activeTaskId``，本文件所有用例在旧实现下均失败。
 *
 * @module tests/ContextUsageRing.activeTask
 */
import { beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { useContextUsageStore } from "@/stores/contextUsageStore";
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

describe("ContextUsageRing 按 activeTaskId 派生", () => {
  // 无 activeTaskId 时不得显示任何 task 的占用。
  it("无活跃任务时显示 0/0，不显示任何 task 的占用", () => {
    useContextUsageStore.getState().setUsage(1, { used_tokens: 90000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");

    render(<ContextUsageRing />);

    expect(document.body.textContent).toContain("0.0%");
    expect(document.body.textContent).not.toContain("90000");
  });

  // 切换 activeTaskId 时圆环读取对应 task 的占用。
  it("切换活跃任务时读取对应 task 的占用", () => {
    const store = useContextUsageStore.getState();
    store.setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");
    store.setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");

    render(<ContextUsageRing />);

    // store 变更必须包在 act 内，否则 React 不会同步刷新（断言读到的是上一次渲染）。
    act(() => {
      useTaskStore.setState({ activeTaskId: 1 });
    });
    expect(document.body.textContent).toContain("0.5%");

    act(() => {
      useTaskStore.setState({ activeTaskId: 2 });
    });
    expect(document.body.textContent).toContain("3.9%");
  });

  // 活跃任务无占用条目时回落 0/0，不得借用其它 task 的占用。
  it("活跃任务无占用数据时回落 0/0，不借用其它 task 的值", () => {
    useContextUsageStore.getState().setUsage(2, { used_tokens: 5000, total_tokens: 128000 }, "2026-08-28T10:00:01Z");
    useTaskStore.setState({ activeTaskId: 1 });

    render(<ContextUsageRing />);

    expect(document.body.textContent).toContain("0.0%");
    expect(document.body.textContent).not.toContain("3.9%");
  });

  // 占用被同一 task 的新事件刷新后，圆环同步更新。
  it("同一 task 的占用刷新后圆环同步更新", () => {
    useTaskStore.setState({ activeTaskId: 1 });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 1000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");

    render(<ContextUsageRing />);
    expect(document.body.textContent).toContain("0.5%");

    act(() => {
      useContextUsageStore
        .getState()
        .setUsage(1, { used_tokens: 100000, total_tokens: 200000 }, "2026-08-28T10:00:02Z");
    });
    expect(document.body.textContent).toContain("50.0%");
  });

  // 占用超限时比例封顶 100%，不出现 >100% 的圆环。
  it("占用超过窗口上限时比例封顶 100%", () => {
    useTaskStore.setState({ activeTaskId: 1 });
    useContextUsageStore.getState().setUsage(1, { used_tokens: 300000, total_tokens: 200000 }, "2026-08-28T10:00:00Z");

    render(<ContextUsageRing />);

    expect(document.body.textContent).toContain("100.0%");
  });
});
