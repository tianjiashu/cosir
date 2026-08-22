// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useSendInput } from "@/components/layout/useSendInput";
import type { UseInputStateReturn } from "@/components/layout/useInputState";
import type { ModelSendGuardResult } from "@/hooks/useModelSendGuard";

// --- mock 副作用依赖，避免真实 localStorage/crypto/trace 噪声 ---
vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    startCurrent: vi.fn(() => ({
      traceId: "perf-test",
      mark: vi.fn(),
    })),
  },
}));

vi.mock("@/services/tracePropagation", () => ({
  beginClientTrace: vi.fn(() => ({ traceId: "client-trace" })),
  endClientTrace: vi.fn(),
}));

vi.mock("@/stores/clientTraceStore", () => ({
  useClientTraceStore: {
    getState: () => ({ currentTrace: null }),
  },
}));

/**
 * 构造一个可发送的输入状态（beginSubmit 永远成功）。
 */
function makeSendableInputState(canStop = false): UseInputStateReturn {
  let submitting = false;
  const state: UseInputStateReturn = {
    phase: "idle",
    canSend: true,
    canStop,
    isSubmitting: false,
    beginSubmit: () => {
      if (submitting) return false;
      submitting = true;
      state.isSubmitting = true;
      return true;
    },
    endSubmit: () => {
      submitting = false;
      state.isSubmitting = false;
    },
  };
  return state;
}

/**
 * 构造默认 params（所有动作可注入 spy）。
 */
function makeParams(overrides: Partial<Parameters<typeof useSendInput>[0]> = {}) {
  return {
    activeTaskId: "task-1",
    activeWorkspaceId: null,
    guardSend: vi.fn<[], Promise<ModelSendGuardResult>>().mockResolvedValue({ ok: true }),
    createTurn: vi.fn<[string], Promise<boolean>>().mockResolvedValue(true),
    createTask: vi.fn<[string, string], Promise<boolean>>().mockResolvedValue(true),
    cancelTurn: vi.fn<[], Promise<void>>().mockResolvedValue(undefined),
    streamingTurnId: null,
    setInputDraft: vi.fn(),
    setGuardMessage: vi.fn(),
    onOpenSettings: vi.fn(),
    ...overrides,
  };
}

describe("useSendInput.send", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("canSend=false 时 send 直接 return，不调用任何发送动作（防御）", async () => {
    const params = makeParams();
    const inputState = makeSendableInputState();
    inputState.canSend = false;
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("hello", { commit, restore });
    });
    expect(params.createTurn).not.toHaveBeenCalled();
    expect(params.setInputDraft).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });

  it("正常发送：乐观清空草稿 + commit + createTurn(true) + 不回滚", async () => {
    const params = makeParams();
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("hello world", { commit, restore });
    });
    expect(params.setInputDraft).toHaveBeenCalledWith("task-1", "");
    expect(commit).toHaveBeenCalledTimes(1);
    expect(params.createTurn).toHaveBeenCalledWith("hello world");
    expect(restore).not.toHaveBeenCalled();
    expect(params.setInputDraft).toHaveBeenCalledTimes(1);
  });

  it("createTurn 返回 false（失败）→ 回滚草稿（setInputDraft 恢复 + local.restore）", async () => {
    const params = makeParams({ createTurn: vi.fn<[string], Promise<boolean>>().mockResolvedValue(false) });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("draft content", { commit, restore });
    });
    expect(params.setInputDraft).toHaveBeenNthCalledWith(1, "task-1", "");
    expect(params.setInputDraft).toHaveBeenNthCalledWith(2, "task-1", "draft content");
    expect(restore).toHaveBeenCalledWith("draft content");
  });

  it("createTurn 抛异常 → 同样回滚草稿", async () => {
    const params = makeParams({
      createTurn: vi.fn<[string], Promise<boolean>>().mockRejectedValue(new Error("boom")),
    });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("draft content", { commit, restore });
    });
    expect(params.setInputDraft).toHaveBeenNthCalledWith(2, "task-1", "draft content");
    expect(restore).toHaveBeenCalledWith("draft content");
  });

  it("guardSend 拦截（ok=false）→ 不清空草稿、不调用 createTurn、setGuardMessage 被调用", async () => {
    const guardResult: ModelSendGuardResult = {
      ok: false,
      block: { reason: "api_key_missing", message: "请配置 API Key", openSettings: true },
    };
    const params = makeParams({
      guardSend: vi.fn<[], Promise<ModelSendGuardResult>>().mockResolvedValue(guardResult),
    });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("hello", { commit, restore });
    });
    expect(params.setGuardMessage).toHaveBeenCalledWith("请配置 API Key");
    expect(params.onOpenSettings).toHaveBeenCalledTimes(1);
    expect(params.setInputDraft).not.toHaveBeenCalled();
    expect(params.createTurn).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
  });

  it("guardSend 拦截且 openSettings=false → 不触发 onOpenSettings", async () => {
    const guardResult: ModelSendGuardResult = {
      ok: false,
      block: { reason: "no_model_selected", message: "请选择模型", openSettings: false },
    };
    const params = makeParams({
      guardSend: vi.fn<[], Promise<ModelSendGuardResult>>().mockResolvedValue(guardResult),
    });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    await act(async () => {
      await result.current.send("hello", { commit: vi.fn(), restore: vi.fn() });
    });
    expect(params.onOpenSettings).not.toHaveBeenCalled();
  });

  it("新建任务场景（activeWorkspaceId 有值，activeTaskId 为 null）→ 调用 createTask", async () => {
    const params = makeParams({ activeTaskId: null, activeWorkspaceId: "ws-1" });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    await act(async () => {
      await result.current.send("new task text", { commit: vi.fn(), restore: vi.fn() });
    });
    expect(params.createTask).toHaveBeenCalledWith("new task text", "ws-1");
    expect(params.createTurn).not.toHaveBeenCalled();
    expect(params.setInputDraft).not.toHaveBeenCalled();
  });

  it("新建任务场景 createTask 失败 → activeTaskId 为 null，不调用 setInputDraft 也不调用 local.restore", async () => {
    const params = makeParams({
      activeTaskId: null,
      activeWorkspaceId: "ws-1",
      createTask: vi.fn<[string, string], Promise<boolean>>().mockResolvedValue(false),
    });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const restore = vi.fn();
    await act(async () => {
      await result.current.send("draft", { commit: vi.fn(), restore });
    });
    // 新建任务场景（activeTaskId 为 null）：无 store 草稿可恢复，故 setInputDraft 不被调用；
    // 但本地输入框已被乐观 commit 清空，失败时必须 restore 恢复本地内容（失败不丢草稿）。
    expect(params.setInputDraft).not.toHaveBeenCalled();
    expect(restore).toHaveBeenCalledWith("draft");
  });
});

describe("useSendInput.stop", () => {
  it("canStop=false 时 stop 直接 return，不调用 cancelTurn", async () => {
    const params = makeParams();
    const inputState = makeSendableInputState(false);
    const { result } = renderHook(() => useSendInput(params, inputState));
    await act(async () => {
      await result.current.stop();
    });
    expect(params.cancelTurn).not.toHaveBeenCalled();
  });

  it("canStop=true 时 stop 调用 cancelTurn", async () => {
    const params = makeParams();
    const inputState = makeSendableInputState(true);
    const { result } = renderHook(() => useSendInput(params, inputState));
    await act(async () => {
      await result.current.stop();
    });
    expect(params.cancelTurn).toHaveBeenCalledTimes(1);
  });
});

/**
 * 提交互斥锁并发语义（放在最后，避免并发 act 警告污染其他用例）。
 * 第一次 send 卡在 guardSend 等待期间，第二次 send 应被 beginSubmit 互斥锁挡下，
 * 不重复调用 createTurn。
 */
describe("useSendInput 提交互斥（并发）", () => {
  it("第一次 send 在飞时第二次 send 被互斥锁挡下（createTurn 仅调用一次）", async () => {
    let resolveGuard: (v: ModelSendGuardResult) => void = () => {};
    const guardPromise = new Promise<ModelSendGuardResult>((res) => (resolveGuard = res));
    const params = makeParams({
      guardSend: vi.fn<[], Promise<ModelSendGuardResult>>().mockReturnValue(guardPromise),
    });
    const inputState = makeSendableInputState();
    const { result } = renderHook(() => useSendInput(params, inputState));
    const commit = vi.fn();
    const restore = vi.fn();

    // 第一次 send 进入 act 但停在 await guardSend（在飞，未 await 完成）
    const first = act(async () => {
      await result.current.send("hello", { commit, restore });
    });
    // 此时第一次仍在 guardSend pending，立即发起第二次 send（应被 beginSubmit 锁挡下）
    const second = act(async () => {
      await result.current.send("hello2", { commit, restore });
    });

    resolveGuard({ ok: true });
    await first;
    await second;

    // 互斥锁保证：createTurn 只执行一次，本地 commit 也只一次
    expect(params.createTurn).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledTimes(1);
  });
});
