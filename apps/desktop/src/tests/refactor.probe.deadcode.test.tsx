// @vitest-environment happy-dom
/**
 * 探查测试（临时）：验证 NewTaskPage 不再渲染任何 agent·model 概要维度。
 *
 * 背景：本次重构（去掉 Agent 选择 UI + 模型选择下沉）删除了 NewTaskPage 中未接线的
 * `summarizeAgentModel` 死函数与 `agentModelSummary` useMemo（含 developer 硬编码分支），
 * 卡片区域不再展示 agent·model 概要（无 card-agent-model testid、无 "Developer · 选择模型" 文案）。
 *
 * 本探查确认：DOM 中不含任何 agent 维度概要文本（"Developer" / "main_agent" / "· 选择模型" 概要）。
 * 注意：本用例渲染 NewTaskPage 受生产代码现状约束（见 NewTaskPage.tsx:191-195 残留 useEffect
 * 引用的 selectedModelName 未定义问题，已由测试 Agent 上报，待主 Agent 修复）；
 * 该渲染期 ReferenceError 修复后将能正常断言无概要。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

vi.mock("@/services/api", () => ({
  listModels: vi.fn().mockResolvedValue([]),
  listProviders: vi.fn().mockResolvedValue([]),
}));
vi.mock("@/lib/logger", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/logger")>()),
  logInfo: vi.fn(), logWarn: vi.fn(), logError: vi.fn(),
}));
vi.mock("@/services/tracePropagation", () => ({ beginClientTrace: vi.fn(), endClientTrace: vi.fn(), hasClientTrace: vi.fn(() => false) }));
vi.mock("@/lib/perf", () => ({ PerfTrace: { markCurrent: vi.fn(), endCurrent: vi.fn(), startCurrent: vi.fn(() => ({ traceId: "t", mark: vi.fn() })) } }));
vi.mock("@/services/workspace", () => ({ pickAndCreateWorkspace: vi.fn() }));
vi.mock("@/hooks/useModelSendGuard", () => ({ useModelSendGuard: () => ({ guardSend: vi.fn().mockResolvedValue({ ok: true }) }) }));
vi.mock("@/hooks/useTask", () => ({ useTask: () => ({ createTask: vi.fn(), createTurn: vi.fn(), cancelTurn: vi.fn(), operation: { loading: false, error: null, eventsError: null } }) }));

import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";

describe("探查：NewTaskPage 不再渲染 agent·model 概要维度", () => {
  it("默认态下 DOM 中无 agent·model 概要（卡片不再含 agent 维度）", () => {
    useWorkspaceStore.setState({
      workspaces: [{ workspace_id: "ws-1", name: "demo", root_path: "/tmp/demo" } as never],
      activeWorkspaceId: "ws-1",
    });
    useTaskStore.setState({ selectedModelName: null, availableModels: [] });
    const { container } = render(<NewTaskPage onCreated={vi.fn()} />);
    const text = container.textContent ?? "";
    // 重构后卡片不再展示 agent·model 概要：
    // 1. 旧的 card-agent-model 概要文案（"Developer · 选择模型"）已随死函数删除而消失；
    // 2. 不应出现 agent id 字符串（main_agent / developer）。
    expect(text).not.toContain("main_agent");
    expect(text).not.toContain("Developer");
    // 模型选择入口仍下沉到底部 ModelSelector（aria-label=选择模型），但不以概要卡片形式出现。
    expect(container.querySelector('[data-testid="card-agent-model"]')).toBeNull();
  });
});
