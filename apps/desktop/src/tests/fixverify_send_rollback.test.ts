/**
 * 修复验证测试：缺陷 3（P1）- 新建任务首 turn 失败时草稿应回填输入框
 *
 * 覆盖点：
 *  1. 失败时用户输入文本被回填回输入框（不丢草稿）
 *  2. 成功时不回填
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// useSendInput 内部使用了 react 的 useCallback（Hook），非组件环境下需将其降级为
// 普通函数包装器，以便直接调用被测 hook 返回的 send。
vi.mock("react", () => ({
  useCallback: (fn: any) => fn,
  useRef: (init: any) => ({ current: init }),
  useState: (init: any) => [typeof init === "function" ? init() : init, vi.fn()],
}));

vi.mock("@/lib/logger", () => ({
  logError: vi.fn(),
  logWarn: vi.fn(),
  logInfo: vi.fn(),
}));
vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(),
  endClientTrace: vi.fn(),
}));
vi.mock("@/stores/clientTraceStore", () => {
  const useClientTraceStore = () => ({ getState: () => ({ currentTrace: { traceId: "x" } }) });
  (useClientTraceStore as any).getState = () => ({ currentTrace: { traceId: "x" } });
  return { useClientTraceStore };
});
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    startCurrent: () => ({ mark: vi.fn(), traceId: "t" }),
  },
}));

import { useSendInput } from "@/components/layout/useSendInput";
import type { ModelSendGuardResult } from "@/hooks/useModelSendGuard";
import type { UseInputStateReturn } from "@/components/layout/useInputState";

function makeGuard(ok: boolean): () => Promise<ModelSendGuardResult> {
  return async () =>
    ok
      ? { ok: true }
      : { ok: false, block: { reason: "model_missing", message: "m", openSettings: true } };
}

/**
 * 构造符合 ``UseInputStateReturn`` 契约的最小提交锁桩。
 *
 * 集中构造以保证字段完整：该接口含 phase / isSubmitting 等 6 个字段，
 * 各处零散写字面量一旦接口增改就会批量失配（此前即因此产生 5 处 TS2345）。
 *
 * @param overrides - 需覆盖的字段（通常只需改 canSend）。
 * @returns 可直接传给 useSendInput 的第二参数。
 */
function makeLockState(
  overrides: Partial<UseInputStateReturn> = {},
): UseInputStateReturn {
  return {
    phase: "idle",
    canSend: true,
    canStop: false,
    isSubmitting: false,
    beginSubmit: () => true,
    endSubmit: vi.fn(),
    ...overrides,
  };
}

describe("缺陷3: useSendInput 失败回填草稿 / 成功不回填", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("新建任务场景（activeTaskId=null, activeWorkspaceId 有值）：createTask 失败时回填草稿到输入框", async () => {
    const guard = makeGuard(true);
    const createTask = vi.fn(async () => false); // 失败
    const createTurn = vi.fn(async () => false);
    const setInputDraft = vi.fn();
    const setGuardMessage = vi.fn();
    const commit = vi.fn();
    const restore = vi.fn();

    const { send } = useSendInput(
      {
        activeTaskId: null,
        activeWorkspaceId: 100,
        guardSend: guard,
        createTurn,
        createTask,
        cancelTurn: vi.fn(async () => {}),
        streamingTurnId: null,
        setInputDraft,
        setGuardMessage,
      },
      makeLockState({ canSend: true }),
    );

    const text = "用户输入了很长的内容";
    await send(text, { commit, restore });

    // 失败必须回填
    expect(restore).toHaveBeenCalledTimes(1);
    expect(restore).toHaveBeenCalledWith(text);
    // 新建任务场景下本地乐观清空 commit 仍被调用
    expect(commit).toHaveBeenCalledTimes(1);
    // 因 activeTaskId 为 null，不应写 store 草稿（避免误清）
    expect(setInputDraft).not.toHaveBeenCalled();
  });

  it("新建任务场景：createTask 成功时不回填（restore 不被调用）", async () => {
    const guard = makeGuard(true);
    const createTask = vi.fn(async () => true); // 成功
    const createTurn = vi.fn(async () => false);
    const setInputDraft = vi.fn();
    const setGuardMessage = vi.fn();
    const commit = vi.fn();
    const restore = vi.fn();

    const { send } = useSendInput(
      {
        activeTaskId: null,
        activeWorkspaceId: 100,
        guardSend: guard,
        createTurn,
        createTask,
        cancelTurn: vi.fn(async () => {}),
        streamingTurnId: null,
        setInputDraft,
        setGuardMessage,
      },
      makeLockState({ canSend: true }),
    );

    await send("成功内容", { commit, restore });
    expect(restore).not.toHaveBeenCalled();
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it("追加轮次场景（activeTaskId 有值）：createTurn 失败时回填草稿到输入框", async () => {
    const guard = makeGuard(true);
    const createTask = vi.fn(async () => false);
    const createTurn = vi.fn(async () => false); // 失败
    const setInputDraft = vi.fn();
    const setGuardMessage = vi.fn();
    const commit = vi.fn();
    const restore = vi.fn();

    const { send } = useSendInput(
      {
        activeTaskId: 42,
        activeWorkspaceId: 100,
        guardSend: guard,
        createTurn,
        createTask,
        cancelTurn: vi.fn(async () => {}),
        streamingTurnId: null,
        setInputDraft,
        setGuardMessage,
      },
      makeLockState({ canSend: true }),
    );

    const text = "轮次失败文本";
    await send(text, { commit, restore });
    expect(restore).toHaveBeenCalledWith(text);
    // 追加轮次场景：先清空 store 草稿
    expect(setInputDraft).toHaveBeenCalledWith(42, "");
  });

  it("模型校验拦截时不应清空/回填草稿（直接 return）", async () => {
    const guard = makeGuard(false); // 拦截
    const createTask = vi.fn(async () => true);
    const createTurn = vi.fn(async () => true);
    const setInputDraft = vi.fn();
    const setGuardMessage = vi.fn();
    const commit = vi.fn();
    const restore = vi.fn();

    const { send } = useSendInput(
      {
        activeTaskId: null,
        activeWorkspaceId: 100,
        guardSend: guard,
        createTurn,
        createTask,
        cancelTurn: vi.fn(async () => {}),
        streamingTurnId: null,
        setInputDraft,
        setGuardMessage,
      },
      makeLockState({ canSend: true }),
    );

    await send("被拦截", { commit, restore });
    expect(createTask).not.toHaveBeenCalled();
    expect(createTurn).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
    expect(setInputDraft).not.toHaveBeenCalled();
  });

  it("canSend=false 时直接拒绝，不触发任何副作用", async () => {
    const guard = makeGuard(true);
    const createTask = vi.fn(async () => true);
    const setInputDraft = vi.fn();
    const setGuardMessage = vi.fn();
    const commit = vi.fn();
    const restore = vi.fn();

    const { send } = useSendInput(
      {
        activeTaskId: null,
        activeWorkspaceId: 100,
        guardSend: guard,
        createTurn: vi.fn(async () => true),
        createTask,
        cancelTurn: vi.fn(async () => {}),
        streamingTurnId: null,
        setInputDraft,
        setGuardMessage,
      },
      makeLockState({ canSend: false }),
    );

    await send("不可发送", { commit, restore });
    expect(createTask).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });
});
