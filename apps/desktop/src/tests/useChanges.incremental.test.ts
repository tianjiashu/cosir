// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

// mock api 模块：useChanges 通过 fetchChangeSet / keepChanges / revertChanges 与后端交互，
// 这里全部替换为可控桩，重点验证「增量游标遍历」下事件消费与去抖行为无回归。
vi.mock("@/services/api", () => ({
  fetchChangeSet: vi.fn(),
  keepChanges: vi.fn(),
  revertChanges: vi.fn(),
}));

import * as api from "@/services/api";
import { useChanges } from "@/hooks/useChanges";
import { useEventStore } from "@/stores/eventStore";
import type { RuntimeEvent } from "@shared/events";
import type { ChangeSet } from "@shared/api";

const TASK_ID = "task-incremental";
const TURN_ID = "turn-1";

interface UpdatedPayload {
  task_id: string;
  turn_id: string;
  path: string;
  action: string;
  additions: number;
  deletions: number;
  before: string | null;
  after: string | null;
}

function makeUpdated(seq: number, path: string): RuntimeEvent {
  const payload: UpdatedPayload = {
    task_id: TASK_ID,
    turn_id: TURN_ID,
    path,
    action: "modified",
    additions: 1,
    deletions: 0,
    before: "",
    after: "x",
  };
  return {
    event_id: `u-${seq}`,
    task_id: TASK_ID,
    turn_id: TURN_ID,
    event_type: "file_change_updated",
    sequence: seq,
    created_at: new Date(seq * 1000).toISOString(),
    payload,
  } as unknown as RuntimeEvent;
}

function makeStable(seq: number): RuntimeEvent {
  return {
    event_id: `s-${seq}`,
    task_id: TASK_ID,
    turn_id: TURN_ID,
    event_type: "file_change_stable",
    sequence: seq,
    created_at: new Date(seq * 1000).toISOString(),
    payload: { task_id: TASK_ID, turn_id: TURN_ID, path: `f-${seq}.txt`, action: "modified" },
  } as unknown as RuntimeEvent;
}

function changeSetWithPaths(paths: string[]): ChangeSet {
  return {
    task_id: TASK_ID,
    checkpoints: [],
    files: paths.map((p) => ({
      path: p,
      action: "modified",
      status: "pending",
      last_tool_call_id: "",
      last_turn_id: TURN_ID,
      additions: 1,
      deletions: 0,
    })),
  };
}

function resetStores() {
  useEventStore.setState({
    events: [],
    eventsByTaskId: {},
    eventsByTurnId: {},
    connectionState: "idle",
    processedEventIds: new Set<string>(),
  });
}

describe("useChanges 增量游标遍历", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    resetStores();
    vi.mocked(api.fetchChangeSet).mockResolvedValue(changeSetWithPaths([]));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("高频 file_change_updated 批量到达：去抖聚合刷新一次，权威数据完整无丢失", async () => {
    const paths: string[] = [];
    // 去抖刷新返回「全部已灌入 path」的权威数据，用于验证高频聚合后仍完整。
    vi.mocked(api.fetchChangeSet).mockImplementation(async () => changeSetWithPaths(paths));

    const { result } = renderHook(() => useChanges(TASK_ID));
    // 初始 taskId-effect 触发一次 refresh。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);

    const batchCount = 5;
    const perBatch = 20;
    // 分 5 批灌入 100 个不同 path 的 updated 事件，模拟工具批量执行的高频推送。
    for (let b = 0; b < batchCount; b++) {
      const batch: RuntimeEvent[] = [];
      for (let i = 0; i < perBatch; i++) {
        const idx = b * perBatch + i;
        const path = `file-${idx}.ts`;
        paths.push(path);
        batch.push(makeUpdated(idx + 1, path));
      }
      await act(async () => {
        useEventStore.getState().setEvents(batch, TASK_ID);
        await vi.advanceTimersByTimeAsync(0);
      });
    }

    // 推进去抖窗口，触发聚合刷新（高频事件应只刷新一次，而非每批一次）。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });

    // 初始 1 次（taskId-effect）+ 去抖聚合 1 次 = 2 次；不随批量数线性增长（非 1+5）。
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(2);

    // 权威刷新返回的数据完整：100 个去重 path 均在变更集中，无丢失。
    const files = result.current.changeSet?.files ?? [];
    expect(files.length).toBe(batchCount * perBatch);
    for (const p of paths) {
      expect(files.some((f) => f.path === p)).toBe(true);
    }
  });

  it("长会话历史 stable 首次全量消费不重复刷新；新增 stable 只触发一次刷新", async () => {
    // 先灌入 200 个历史 stable 事件，模拟「先渲染后回填」的回放场景。
    const history: RuntimeEvent[] = [];
    for (let i = 1; i <= 200; i++) {
      history.push(makeStable(i));
    }
    useEventStore.getState().setEvents(history, TASK_ID);

    const { result } = renderHook(() => useChanges(TASK_ID));
    // 初始 taskId-effect 触发一次刷新；首次历史 stable 应被全量消费且不额外刷新。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    // 历史 200 个被初始化分支消费，不触发刷新；仅初始 refresh 1 次。
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);

    // 追加第 201 个 stable：应仅触发一次刷新（不重复处理历史 200 个，游标增量只处理新增）。
    await act(async () => {
      useEventStore.getState().setEvents([makeStable(201)], TASK_ID);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(2);
    expect(result.current.changeSet).not.toBeNull();
  });

  it("taskEvents 被整体替换（重连补灌）时回退全量，聚合刷新一次", async () => {
    const paths: string[] = [];
    // 去抖刷新返回权威完整数据，验证整体替换识别后聚合为单次刷新且数据完整。
    vi.mocked(api.fetchChangeSet).mockImplementation(async () => changeSetWithPaths(paths));

    // 先灌入 50 个 updated 事件（seq 1..50，首事件 u-1）。
    const first: RuntimeEvent[] = [];
    for (let i = 1; i <= 50; i++) {
      const p = `a-${i}.ts`;
      paths.push(p);
      first.push(makeUpdated(i, p));
    }
    useEventStore.getState().setEvents(first, TASK_ID);

    const { result } = renderHook(() => useChanges(TASK_ID));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);

    // 整体替换：长度不增（仍为 50），但首事件从 u-1 变为 u-2（seq 1 被替换、seq 51 新增），
    // 命中「首事件身份变化」回退分支（区别于用例 4 的长度增长场景）。游标回退全量重扫应处理新增。
    const replaced: RuntimeEvent[] = [];
    for (let i = 2; i <= 50; i++) {
      replaced.push(makeUpdated(i, `a-${i}.ts`));
    }
    const newPath = "a-51.ts";
    paths.push(newPath);
    replaced.push(makeUpdated(51, newPath)); // 1 个真正新增
    // setEvents 与去抖推进分两个 act：先让 updated effect 刷新（设去抖定时器），再推进窗口触发刷新。
    await act(async () => {
      useEventStore.getState().setEvents(replaced, TASK_ID);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });

    // 整体替换触发一次聚合刷新（去抖），权威数据完整含新增 a-51.ts。
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(2);
    const files = result.current.changeSet?.files ?? [];
    expect(files.some((f) => f.path === newPath)).toBe(true);
    expect(files.length).toBe(51);
  });

  it("头部插入更早历史（长度不减反增）时游标回退，不漏处理新增事件", async () => {
    // 模拟同 task 内 openTask(forceRefresh=true) 在头部插入更早历史事件：
    // 已有 10 个 updated（seq 11..20），再 setEvents 灌入更早的 seq 1..10，数组变为 20 长，
    // 首事件身份变化但长度不减反增——仅靠长度无法识别，必须靠首事件身份回退全量。
    const paths: string[] = [];
    vi.mocked(api.fetchChangeSet).mockImplementation(async () => changeSetWithPaths(paths));

    const initial: RuntimeEvent[] = [];
    for (let i = 11; i <= 20; i++) {
      const p = `h-${i}.ts`;
      paths.push(p);
      initial.push(makeUpdated(i, p));
    }
    useEventStore.getState().setEvents(initial, TASK_ID);

    const { result } = renderHook(() => useChanges(TASK_ID));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);

    // 头部插入 seq 1..10（更早历史）+ 追加真正新增 seq 21。
    const prepended: RuntimeEvent[] = [];
    for (let i = 1; i <= 10; i++) {
      const p = `h-${i}.ts`;
      paths.push(p);
      prepended.push(makeUpdated(i, p));
    }
    for (let i = 11; i <= 20; i++) {
      prepended.push(makeUpdated(i, `h-${i}.ts`));
    }
    const newPath = "h-21.ts";
    paths.push(newPath);
    prepended.push(makeUpdated(21, newPath));
    // setEvents 与去抖推进分两个 act：先让 updated effect 刷新（设去抖定时器），再推进窗口触发刷新。
    await act(async () => {
      useEventStore.getState().setEvents(prepended, TASK_ID);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });

    // 整体替换（首事件身份变化，长度不减反增）触发回退全量，聚合为单次刷新；
    // 权威数据含全部 21 个路径（含头部插入的 h-1..h-10 与新增 h-21），不因游标停留漏处理。
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(2);
    const files = result.current.changeSet?.files ?? [];
    expect(files.length).toBe(21);
    for (const p of paths) {
      expect(files.some((f) => f.path === p)).toBe(true);
    }
  });
});
