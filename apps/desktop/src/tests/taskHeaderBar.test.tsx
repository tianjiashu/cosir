// @vitest-environment happy-dom
/**
 * TaskHeaderBar 组件测试（方案 A：抽公共组件）。
 *
 * 守护不变量：
 * 1. 组件挂载即渲染 AgentSelector / ModelSelector 触发按钮；
 * 2. 选择 Agent / Model 时直接更新 useTaskStore（去除 value/onChange props 后无 props 入口）；
 * 3. 点击 ModelSelector 的「配置模型 / 管理厂商」入口打开 ProviderSettingsDialog，
 *    关闭对话框即收起；
 * 4. 默认态（store 初始值）下折叠态分别显示 `Developer`（fallback agent）与
 *    `选择模型`（null = 未选择，无 Auto 语义，2026-08-18 起）。
 *
 * @module tests/taskHeaderBar
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// listAgents / listModels 是网络依赖，桩化避免测试环境连真实后端；
// 本测试关注组件交互与 store 写入，不关注网络。
// 用 vi.hoisted + vi.mocked 暴露可按用例覆盖返回值的 vi.fn()，便于补「agent 点击写 store」用例。
const listAgentsMock = vi.hoisted(() => vi.fn().mockResolvedValue({ agents: [], default_agent_id: "developer" }));
const listModelsMock = vi.hoisted(() => vi.fn().mockResolvedValue([]));
vi.mock("@/services/api", () => ({
  listAgents: listAgentsMock,
  listModels: listModelsMock,
}));
// logger 噪声屏蔽。
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
}));

import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
import { useTaskStore } from "@/stores/taskStore";
import { useAgentStore } from "@/stores/agentStore";
import * as apiModule from "@/services/api";
import type { AgentProfileResponse } from "@shared/api";
import type { ModelEntryRecord } from "@shared/model";

function makeAgent(overrides: Partial<AgentProfileResponse> = {}): AgentProfileResponse {
  return {
    agent_id: "developer",
    role: "Developer",
    description: "通用开发",
    allowed_tools: [],
    workflow: "react",
    model_name: "deepseek/deepseek-v4-flash",
    max_steps: 25,
    prompt_ref: null,
    ...overrides,
  };
}

function makeModel(overrides: Partial<ModelEntryRecord> = {}): ModelEntryRecord {
  return {
    model_id: "model-1",
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: "deepseek/deepseek-v4-flash",
    display_name: "deepseek-v4-flash",
    max_context_window: 131072,
    supports_thinking: true,
    temperature: null,
    top_p: null,
    max_tokens: null,
    enabled: true,
    api_key_configured: true,
    sort_order: 0,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    ...overrides,
  };
}

describe("TaskHeaderBar", () => {
  beforeEach(() => {
    cleanup();
    // 重置 store 到初始默认态：selectedAgentId=developer, selectedModelName=null (未选择)。
    useTaskStore.setState({
      selectedAgentId: "developer",
      selectedModelName: null,
      availableModels: [],
      modelsLoaded: true,
    });
    // 每个用例前把 listAgents mock 重置为「无 agent」默认态；
    // 显式覆盖返回值的用例在内部再单独设置一次。
    listAgentsMock.mockResolvedValue({ agents: [], default_agent_id: "developer" });
    // agentStore 是模块级单例，loaded 跨用例持久；重置后每次挂载 AgentSelector
    // 才会真正走 refreshAgents → listAgents（让「点击 agent 选项」用例可控）。
    useAgentStore.setState({ agents: [], defaultAgentId: "developer", loaded: false });
  });

  // 守护不变量 1：组件挂载后渲染 AgentSelector（Bot 图标）与 ModelSelector（aria-label 选择模型）入口。
  it("挂载时同时渲染 Agent 与 Model 选择器入口", () => {
    render(<TaskHeaderBar />);
    expect(screen.getByTestId("task-header-bar")).toBeTruthy();
    // AgentSelector 折叠态显示 Bot 图标 + Developer 文字。
    expect(screen.getByText("Developer")).toBeTruthy();
    // ModelSelector 折叠态按钮带 aria-label="选择模型"。
    expect(screen.getByRole("button", { name: "选择模型" })).toBeTruthy();
  });

  // 守护不变量 4（默认态）：折叠态默认显示「Developer · 选择模型」。
  it("默认态折叠标签分别为「Developer」「选择模型」", () => {
    render(<TaskHeaderBar />);
    // AgentSelector 折叠标签：developer → Developer。
    expect(screen.getByText("Developer")).toBeTruthy();
    // ModelSelector 折叠标签：null → 选择模型。
    const modelButton = screen.getByRole("button", { name: "选择模型" });
    expect(modelButton.textContent).toContain("选择模型");
  });

  // 守护不变量 2：选择 Model 触发 store 更新（null → 显式 model）。
  it("选择模型写入 useTaskStore.selectedModelName", () => {
    useTaskStore.setState({
      availableModels: [makeModel()],
    });
    render(<TaskHeaderBar />);
    const modelButton = screen.getByRole("button", { name: "选择模型" });
    fireEvent.click(modelButton);
    fireEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(useTaskStore.getState().selectedModelName).toBe("deepseek/deepseek-v4-flash");
  });

  // 守护不变量 2：选择 Agent 触发 store 更新（AgentSelector 直接读 store）。
  // 此处走「无 agents 列表时的 fallback 显示」路径：agents 为空时折叠标签 = 当前 value
  // （developer → "Developer"，否则回退到 agent_id），验证不再要求外部 props 即可
  // 与 store 同步，无需触发 /agents 网络拉取的复杂时序。
  it("agents 列表为空时折叠标签按 store.selectedAgentId 回退显示（无外部 props 依赖）", () => {
    useTaskStore.setState({ selectedAgentId: "custom-agent" });
    render(<TaskHeaderBar />);
    // 未匹配到 role，回退到 agent_id 字符串。
    expect(screen.getByText("custom-agent")).toBeTruthy();
  });

  // 守护不变量 2（正向覆盖）：listAgents 返回非空 agents 时，点击 AgentSelector 中的
  // agent 选项（按 role 文本定位）应直接写入 useTaskStore.selectedAgentId。
  // 修复：原先 listAgents 永远返回空数组，导致「点 agent 选项 → store 写入」路径
  // 从未真正被覆盖；本用例覆盖该路径，闭合「点击 → 写入 store」契约测试盲点。
  it("点击 agent 选项会写入 useTaskStore.selectedAgentId", async () => {
    // 覆盖 mock：返回 1 个非 fallback agent，验证从默认 developer 切到 researcher 的写入。
    vi.mocked(apiModule.listAgents).mockResolvedValueOnce({
      agents: [
        makeAgent({
          agent_id: "researcher",
          role: "Researcher",
          model_name: "openai/gpt-4o",
        }),
      ],
      default_agent_id: "developer",
    });

    render(<TaskHeaderBar />);

    // 打开 AgentSelector 折叠态按钮（无 aria-label，以 Bot 图标 + 当前 role 文本定位）。
    // 触发后 /agents 已 resolve（happy-dom 下 useEffect 即时执行），下拉应渲染 agent 列表。
    const trigger = screen.getByText("Developer").closest("button");
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger as HTMLElement);

    // 选项是普通 <button>，无 role=option；按 role 文本精确匹配 Researcher。
    const option = await screen.findByText("Researcher");
    fireEvent.click(option);

    expect(useTaskStore.getState().selectedAgentId).toBe("researcher");
  });

  // 守护不变量 3：ModelSelector 的 footer 「配置模型 / 管理厂商」入口打开 ProviderSettingsDialog。
  it("点击 ModelSelector 的「配置模型 / 管理厂商」打开 ProviderSettingsDialog", () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<TaskHeaderBar />);
    const modelButton = screen.getByRole("button", { name: "选择模型" });
    fireEvent.click(modelButton);
    // 底部 footer。
    const footer = screen.getByText("配置模型 / 管理厂商");
    fireEvent.click(footer);
    // 对话框以 DialogTitle 渲染「模型厂商配置」等可识别文案——此处断言出现 dialog 角色。
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  // 守护不变量 3：关闭对话框（按 Radix 的 sr-only 关闭按钮）即收起。
  // DialogClose 渲染 sr-only "关闭" 文本，accessible name 透传给按钮。
  it("ProviderSettingsDialog 可关闭：点击右上角关闭按钮后 dialog 消失", async () => {
    useTaskStore.setState({ availableModels: [makeModel()] });
    render(<TaskHeaderBar />);
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.click(screen.getByText("配置模型 / 管理厂商"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    const closeButton = await screen.findByRole("button", { name: "关闭" });
    fireEvent.click(closeButton);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
