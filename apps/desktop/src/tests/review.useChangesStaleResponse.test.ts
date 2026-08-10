// @vitest-environment happy-dom
/**
 * 缺陷验证 #5：useChanges.refresh() 不过期校验，迟到响应覆盖新任务数据。
 *
 * 背景：refresh()（useChanges.ts 约 106-123 行）`await api.fetchChangeSet(...)` 之后
 * 无条件 `setChangeSet(data)`，不校验发起请求时的 taskId 是否仍是当前 taskId。
 * 时序：打开 task A（请求在途）→ 切到 task B（B 立即返回并展示）→ A 的迟到响应
 * 返回 → 面板被覆盖为 A 的变更集，用户看到的是别的任务的数据。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

// mock api 模块：fetchChangeSet 替换为可控桩，A 手动延迟 resolve、B 立即 resolve。
vi.mock("@/services/api", () => ({
  fetchChangeSet: vi.fn(),
  keepChanges: vi.fn(),
  revertChanges: vi.fn(),
}));

import * as api from "@/services/api";
import { useChanges } from "@/hooks/useChanges";
import { useEventStore } from "@/stores/eventStore";
import type { ChangeSet } from "@shared/api";

function makeChangeSet(taskId: string, filePath: string): ChangeSet {
  return {
    task_id: taskId,
    checkpoints: [],
    files: [
      {
        path: filePath,
        action: "modified",
        status: "pending",
        last_tool_call_id: "",
        last_turn_id: "turn-1",
        additions: 1,
        deletions: 0,
      },
    ],
  } as unknown as ChangeSet;
}

describe("useChanges 过期响应防护", () => {
  const changeSetA = makeChangeSet("task-a", "a-only.ts");
  const changeSetB = makeChangeSet("task-b", "b-only.ts");
  let resolveA: (value: ChangeSet) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    useEventStore.setState({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      processedEventIds: new Set<string>(),
    } as never);
    vi.mocked(api.fetchChangeSet).mockImplementation((taskId: string) => {
      if (taskId === "task-a") {
        return new Promise<ChangeSet>((resolve) => {
          resolveA = resolve;
        });
      }
      return Promise.resolve(changeSetB);
    });
  });

  // 测试目的：task A 请求在途时切换到 task B，A 的迟到响应不应覆盖 B 的数据。
  // 可能发现的缺陷：refresh 缺少 taskId 过期校验（stale response guard），
  //   迟到响应把面板状态回写成旧任务数据。
  it("切换到 task B 后，task A 的迟到响应不应覆盖面板中的 B 数据", async () => {
    const { result, rerender } = renderHook(
      ({ taskId }: { taskId: string }) => useChanges(taskId),
      { initialProps: { taskId: "task-a" } },
    );
    // 初始 effect 已发出 task-a 请求（在途未返回）。
    expect(api.fetchChangeSet).toHaveBeenCalledWith("task-a", undefined);

    // 切换到 task B：B 的响应立即返回并展示。
    await act(async () => {
      rerender({ taskId: "task-b" });
    });
    expect(api.fetchChangeSet).toHaveBeenCalledWith("task-b", undefined);
    expect(result.current.changeSet?.task_id).toBe("task-b");

    // task A 的迟到响应此刻才返回。
    await act(async () => {
      resolveA(changeSetA);
      await Promise.resolve();
    });

    // 正确行为：A 的响应已过期，应被丢弃；面板仍展示 B。
    expect(result.current.changeSet?.task_id).toBe("task-b");
    expect(result.current.changeSet?.files.map((f) => f.path)).toEqual(["b-only.ts"]);
  });

  // 测试目的：同任务内切换 checkpoint（C1→C2）时，C1 的迟到响应不应覆盖 C2 数据。
  // 可能发现的缺陷：过期防护只比对 taskId 快照，同任务内检查点切换的竞态未覆盖，
  //   旧检查点过滤结果会覆盖新检查点视图且面板与选择器不一致。
  it("同任务内切换 checkpoint 后，旧检查点的迟到响应不应覆盖新数据", async () => {
    const changeSetC2 = makeChangeSet("task-a", "c2-only.ts");
    let resolveC1: (value: ChangeSet) => void = () => undefined;
    vi.mocked(api.fetchChangeSet).mockImplementation((_taskId: string, checkpoint?: string) => {
      if (checkpoint === "turn-c2") {
        return Promise.resolve(changeSetC2);
      }
      return new Promise<ChangeSet>((resolve) => {
        resolveC1 = resolve;
      });
    });

    const { result } = renderHook(() => useChanges("task-a"));
    // 初始刷新携带空检查点（C1，在途未返回）。
    expect(api.fetchChangeSet).toHaveBeenCalledWith("task-a", undefined);

    // 切换到 C2：C2 响应立即返回并展示。
    await act(async () => {
      result.current.setCheckpoint("turn-c2");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.changeSet?.files.map((f) => f.path)).toEqual(["c2-only.ts"]);

    // C1 的迟到响应此刻才返回。
    await act(async () => {
      resolveC1(changeSetA);
      await Promise.resolve();
    });

    // 正确行为：C1 响应已过期（检查点已切换），应被丢弃；面板仍展示 C2。
    expect(result.current.changeSet?.files.map((f) => f.path)).toEqual(["c2-only.ts"]);
  });

  // 测试目的：正向对照——未切换任务时，A 的响应正常落地（证明 harness 数据通路正确）。
  // 可能发现的缺陷：无（此用例应 PASS；若失败说明 mock/时序构造有误）。
  it("对照：未切换任务时，task A 的响应正常写入 changeSet", async () => {
    const { result } = renderHook(() => useChanges("task-a"));
    await act(async () => {
      resolveA(changeSetA);
      await Promise.resolve();
    });
    expect(result.current.changeSet?.task_id).toBe("task-a");
    expect(result.current.changeSet?.files.map((f) => f.path)).toEqual(["a-only.ts"]);
  });
});
