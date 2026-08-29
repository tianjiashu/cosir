// @vitest-environment happy-dom
/**
 * InputBar 附件能力组件行为测试（重构新增能力的端到端验证）。
 *
 * 借助对 @assistant-ui/react / useTask / useModelSendGuard / dialog / useSendInput 的 mock，
 * 在组件层验证：
 * 1) ComposerPrimitive 仍被实际使用（未退回自研 textarea）；
 * 2) 发送前 validateForSend 校验失败（URL 协议 / 模型不支持图片 / 数量超限）→ notice 阻断，不进入 send；
 * 3) 合法发送 → 附件传给 createTurn 且清空附件区；
 * 4) 发送失败 → 附件经 restoreAttachments 恢复（chip 仍存在），不丢附件；
 * 5) AttachmentChip 渲染（图标/标签/非图片提示/移除）。
 *
 * 复用既有 sendPath 测试的 mock 约定，但不修改任何生产代码。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useTaskStore } from "@/stores/taskStore";
import { useTurnStore } from "@/stores/turnStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const taskMocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  createTurn: vi.fn(),
  cancelTurn: vi.fn(),
}));
const guardSendMock = vi.hoisted(() => vi.fn());
const selectAttachmentPathsMock = vi.hoisted(() => vi.fn());
const selectDirectoryMock = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useTask", () => ({
  useTask: () => ({
    createTask: taskMocks.createTask,
    createTurn: taskMocks.createTurn,
    cancelTurn: taskMocks.cancelTurn,
    operation: { loading: false, error: null, eventsError: null },
  }),
}));
vi.mock("@/hooks/useModelSendGuard", () => ({
  useModelSendGuard: () => ({ guardSend: guardSendMock }),
}));
vi.mock("@/services/dialog", () => ({
  selectDirectory: selectDirectoryMock,
  selectAttachmentPaths: selectAttachmentPathsMock,
}));
vi.mock("@assistant-ui/react", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  const MockRoot = ({ children, ...props }: React.FormHTMLAttributes<HTMLFormElement>) => (
    <form data-testid="assistant-ui-composer-root" {...props}>
      {children}
    </form>
  );
  const MockInput = React.forwardRef<
    HTMLTextAreaElement,
    React.TextareaHTMLAttributes<HTMLTextAreaElement> & {
      addAttachmentOnPaste?: boolean;
      maxRows?: number;
      minRows?: number;
      submitMode?: string;
    }
  >(({ addAttachmentOnPaste: _addAttachmentOnPaste, maxRows: _maxRows, minRows: _minRows, submitMode: _submitMode, ...props }, ref) => (
    <textarea data-testid="assistant-ui-composer-input" ref={ref} {...props} />
  ));
  const MockSend = React.forwardRef<
    HTMLButtonElement,
    React.ButtonHTMLAttributes<HTMLButtonElement> & { asChild?: boolean }
  >(({ asChild, children, ...props }, ref) => {
    if (asChild && React.isValidElement(children)) {
      return React.cloneElement(children, {
        ...props,
        "data-testid": "assistant-ui-composer-send",
        ref,
      } as React.HTMLAttributes<HTMLButtonElement>);
    }
    return (
      <button data-testid="assistant-ui-composer-send" ref={ref} type="submit" {...props}>
        {children}
      </button>
    );
  });

  return {
    AssistantRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    ComposerPrimitive: {
      Root: MockRoot,
      Input: MockInput,
      Send: MockSend,
    },
    unstable_useComposerInput: vi.fn(() => ({
      canSend: true,
      isDisabled: false,
      send: vi.fn(),
      setText: vi.fn(),
      value: "",
    })),
    useLocalRuntime: vi.fn(() => ({ runtime: "mock-assistant-ui-runtime" })),
  };
});
vi.mock("@/services/tracePropagation", () => ({ beginClientTrace: vi.fn(), endClientTrace: vi.fn() }));
vi.mock("@/lib/perf", () => ({
  PerfTrace: {
    markCurrent: vi.fn(),
    endCurrent: vi.fn(),
    startCurrent: vi.fn(() => ({ traceId: "trace-stub", mark: vi.fn() })),
  },
}));
vi.mock("@/lib/logger", () => ({ logInfo: vi.fn(), logWarn: vi.fn(), logError: vi.fn(), logDebug: vi.fn() }));

import { InputBar } from "@/components/layout/InputBar";

function makeConfiguredModel(overrides: Record<string, unknown> = {}) {
  return {
    model_id: "model-1",
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: "deepseek/deepseek-v4-flash",
    display_name: "deepseek-v4-flash",
    max_context_window: 128000,
    supports_thinking: false,
    temperature: null,
    top_p: null,
    max_tokens: null,
    enabled: true,
    api_key_configured: true,
    sort_order: 0,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    ...overrides,
  } as never;
}

function resetStores() {
  useTaskStore.setState({
    tasksById: {},
    tasksByWorkspaceId: {},
    loadedWorkspaceIds: new Set(),
    activeTaskId: null,
    activeTurnId: null,
    selectedModelName: null,
    availableModels: [],
    modelsLoaded: true,
  });
  useWorkspaceStore.setState({
    workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
    activeWorkspaceId: "ws-1",
  });
  useTurnStore.setState({ turnsByTaskId: {}, streamingTurnIds: {} });
}

beforeEach(() => {
  resetStores();
  vi.clearAllMocks();
  taskMocks.createTask.mockResolvedValue(true);
  taskMocks.createTurn.mockResolvedValue(true);
  guardSendMock.mockReset();
  guardSendMock.mockResolvedValue({ ok: true });
  selectAttachmentPathsMock.mockResolvedValue([]);
  selectDirectoryMock.mockResolvedValue(undefined);
});

/** 渲染一个已选支持图片模型、有 activeTask 的 InputBar，返回 input 与公共工具。 */
function renderInputBar(overrides: Record<string, unknown> = {}) {
  useTaskStore.setState({
    activeTaskId: "t-1",
    selectedModelName: "deepseek/deepseek-v4-flash",
    availableModels: [makeConfiguredModel({ supports_image: true, ...overrides })],
    modelsLoaded: true,
  });
  const utils = render(<InputBar />);
  const input = screen.getByPlaceholderText("给 Agent 下达任务...");
  const sendButton = screen.getByRole("button", { name: "发送" });
  return { input, sendButton, ...utils };
}

describe("ComposerPrimitive 仍被实际使用", () => {
  // 测试目的：证明 InputBar 仍接入 assistant-ui ComposerPrimitive.Root/Input/Send，未退回自研 textarea。
  it("渲染 assistant-ui composer root/input/send 结构", () => {
    renderInputBar();
    expect(screen.getByTestId("assistant-ui-composer-root")).toBeTruthy();
    expect(screen.getByTestId("assistant-ui-composer-input")).toBeTruthy();
    expect(screen.getByTestId("assistant-ui-composer-send")).toBeTruthy();
  });
});

describe("URL 附件校验（发送前拦截）", () => {
  // 测试目的：含非法 url 附件时，handleSend 经 notice 阻断、不调用 createTurn。
  it("非法 url（ftp://）→ 展示 http(s) 提示且 createTurn 不被调", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    // 通过 prompt 录入 URL；mock window.prompt 返回非法 url。
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("ftp://example.com/x");
    const { input } = renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加 URL 附件" }));
    // 校验 chip 已渲染（attachmentLabel 取 URL 末段 "x"，title 保留完整 ref）。
    await waitFor(() => expect(screen.getByTitle("ftp://example.com/x")).toBeTruthy());

    fireEvent.change(input, { target: { value: "hi" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/http\(s\):\/\//)).toBeTruthy());
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  // 测试目的：合法 https url 通过校验并正常发送，createTurn 收到 url 附件。
  it("合法 https url → 通过校验并发送", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("https://example.com/doc");
    const { input } = renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加 URL 附件" }));
    // 校验 chip 已渲染（attachmentLabel 取 URL 末段 "doc"，title 保留完整 ref）。
    await waitFor(() => expect(screen.getByTitle("https://example.com/doc")).toBeTruthy());

    fireEvent.change(input, { target: { value: "fetch" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(taskMocks.createTurn).toHaveBeenCalledWith("fetch", [
        { kind: "url", ref: "https://example.com/doc" },
      ]),
    );
    promptSpy.mockRestore();
  });
});

describe("图片模型能力拦截", () => {
  // 测试目的：当前模型 supports_image=false 且含 image 附件 → 阻断并提示不支持图片。
  it("supports_image=false 且含 image → notice 阻断，createTurn 不调", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    selectAttachmentPathsMock.mockResolvedValue(["H:\\coding-agent\\assets\\shot.png"]);
    const { input } = renderInputBar({ supports_image: false });
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("shot.png")).toBeTruthy());

    fireEvent.change(input, { target: { value: "look" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/不支持图片/)).toBeTruthy());
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
  });

  // 测试目的：supports_image=true 且含 image → 正常发送（反例，证明拦截条件精确）。
  it("supports_image=true 且含 image → 正常发送", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    selectAttachmentPathsMock.mockResolvedValue(["H:\\coding-agent\\assets\\shot.png"]);
    const { input } = renderInputBar({ supports_image: true });
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("shot.png")).toBeTruthy());

    fireEvent.change(input, { target: { value: "look" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(taskMocks.createTurn).toHaveBeenCalledWith("look", [
        { kind: "image", ref: "H:\\coding-agent\\assets\\shot.png" },
      ]),
    );
  });
});

describe("附件数量上限拦截", () => {
  // 测试目的：>20 个附件（全部 image）时发送被阻断，notice 提示数量超限。
  it("21 个附件 → notice 阻断且 createTurn 不调", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    const paths = Array.from({ length: 21 }, (_, i) => `C:\\tmp\\img${i}.png`);
    selectAttachmentPathsMock.mockResolvedValue(paths);
    const { input } = renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getAllByText(/img0.png/).length).toBeGreaterThan(0));

    fireEvent.change(input, { target: { value: "many" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByText(/不能超过 20 个/)).toBeTruthy());
    expect(taskMocks.createTurn).not.toHaveBeenCalled();
  });
});

describe("移除单个附件", () => {
  // 测试目的：点击 chip 移除按钮按 ref 移除，附件数量减少、对应 label 消失。
  it("移除一个附件 → 该附件 chip 消失，剩余仍可见", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    selectAttachmentPathsMock.mockResolvedValue(["/a/one.png", "/b/two.png"]);
    renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("one.png")).toBeTruthy());
    expect(screen.getByText("two.png")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "移除附件 two.png" }));
    await waitFor(() => expect(screen.queryByText("two.png")).toBeNull());
    expect(screen.getByText("one.png")).toBeTruthy();
  });
});

describe("成功清空 / 失败恢复", () => {
  // 测试目的：发送成功 → 附件区清空（chip 消失），证明 commit 调用了 clear。
  it("发送成功 → 附件被清空（chip 消失）", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    taskMocks.createTurn.mockResolvedValue(true);
    selectAttachmentPathsMock.mockResolvedValue(["H:\\coding-agent\\assets\\screen.png"]);
    const { input } = renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("screen.png")).toBeTruthy());

    fireEvent.change(input, { target: { value: "look at this" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(taskMocks.createTurn).toHaveBeenCalledWith("look at this", [
        { kind: "image", ref: "H:\\coding-agent\\assets\\screen.png" },
      ]),
    );
    // 成功后附件应被清空
    await waitFor(() => expect(screen.queryByText("screen.png")).toBeNull());
  });

  // 测试目的：sendInput/createTurn 失败 → 附件经 restoreAttachments 恢复，chip 仍存在。
  it("发送失败（createTurn=false）→ 附件恢复，chip 仍存在", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    taskMocks.createTurn.mockResolvedValue(false);
    selectAttachmentPathsMock.mockResolvedValue(["H:\\coding-agent\\assets\\screen.png"]);
    const { input } = renderInputBar();
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("screen.png")).toBeTruthy());

    fireEvent.change(input, { target: { value: "look at this" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(taskMocks.createTurn).toHaveBeenCalledWith("look at this", [
        { kind: "image", ref: "H:\\coding-agent\\assets\\screen.png" },
      ]),
    );
    // 失败回滚：附件应被恢复，chip 仍存在
    await waitFor(() => expect(screen.getByText("screen.png")).toBeTruthy());
  });
});

describe("AttachmentChip 渲染", () => {
  // 测试目的：非图片附件（url/file/directory）展示「Agent 按需读取」提示；image 不展示。
  it("url 附件展示按需读取提示，image 附件不展示", async () => {
    guardSendMock.mockResolvedValue({ ok: true });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("https://example.com/doc");
    const { input } = renderInputBar();
    // 先加 url 附件
    fireEvent.click(screen.getByRole("button", { name: "添加 URL 附件" }));
    await waitFor(() => expect(screen.getByTitle("https://example.com/doc")).toBeTruthy());
    expect(screen.getByText(/Agent 按需读取/)).toBeTruthy();
    promptSpy.mockRestore();
    // 再加 image 附件覆盖输入，验证 image 不带该提示
    selectAttachmentPathsMock.mockResolvedValue(["/x/photo.png"]);
    fireEvent.click(screen.getByRole("button", { name: "添加文件或图片" }));
    await waitFor(() => expect(screen.getByText("photo.png")).toBeTruthy());
    // image chip 不应再有第二个「按需读取」提示（仍保留 url 的那条）
    expect(screen.getAllByText(/Agent 按需读取/).length).toBe(1);
    void input;
  });
});
