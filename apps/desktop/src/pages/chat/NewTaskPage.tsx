/**
 * 新建任务页面。
 *
 * 负责选择 workspace、显示 task 维度元数据选择（Agent / Model，由 TaskHeaderBar 提供）、
 * 输入首条消息并触发 task + first turn 创建。Agent / Model 选择与 Chat 视图共享同一
 * 事实源（useTaskStore.selectedAgentId / selectedModelName），保证「将要创建的任务」
 * 与「已在聊的任务」配置一致时无感知差异。
 *
 * 模型必须显式选择（无 Auto 语义）：发送前经 `useModelSendGuard.guardSend()` 拦截，
 * 未配置模型 / 未选择模型时阻止创建任务，并引导打开厂商配置中心或提示选择模型。
 *
 * 布局参考 Codex 风格：顶部 TaskHeaderBar + 居中标题 + 四象限快捷入口卡片
 * （卡片右下角展示当前 agent·model 概要，让用户看到「卡片会用此配置创建」） +
 * 底部输入区 + 工作区选择器。
 *
 * 工作区选择直接调用系统目录选择器，不再要求手动输入路径。
 *
 * @module pages/chat/NewTaskPage
 */

import { useState, useRef, useEffect, useMemo } from "react";
import {
  Send,
  Search,
  Wrench,
  ClipboardList,
  Bug,
  FolderOpen,
  Plus,
  ChevronDown,
  ChevronUp,
  Check,
  Folder,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTask } from "@/hooks/useTask";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";
import { logError, logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { pickAndCreateWorkspace } from "@/services/workspace";
import { TaskHeaderBar } from "@/components/chat/TaskHeaderBar";
import { useModelSendGuard } from "@/hooks/useModelSendGuard";
import type { ModelEntryRecord } from "@shared/model";

/** NewTaskPage 组件属性。 */
interface NewTaskPageProps {
  /** 首条消息发送成功后的回调。 */
  onCreated: () => void;
}

/** 快捷入口卡片配置。 */
const SUGGESTIONS = [
  { icon: Search, label: "探索并理解代码" },
  { icon: Wrench, label: "构建新功能、应用或工具" },
  { icon: ClipboardList, label: "审查代码并提出修改建议" },
  { icon: Bug, label: "修复问题和失败" },
] as const;

/**
 * 组装 agent·model 概要文本（如 `Developer · 选择模型`），用于 4 象限卡片右下角。
 *
 * 纯函数：不订阅 store、不调用 React hooks；订阅发生在 NewTaskPage 顶层。
 * 该命名刻意去掉 `use` 前缀，避免被识别为 React Hook 触发 lint 误报与规则误用。
 *
 * Agent 折叠标签与 AgentSelector 同源（developer → Developer；其他 agent 默认回显 agent_id）；
 * Model 折叠标签与 ModelSelector 同源（null 固定「选择模型」，否则取 display_name）。
 *
 * @param params - 输入参数集合。
 * @param params.agentId - 当前选中的 agent_id（可能为 null/falsy）。
 * @param params.modelName - 当前选中的模型名（null = 未选择，需显式选择）。
 * @param params.availableModels - 后端可用的模型列表，用于查 display_name。
 * @returns 形如 `Developer · 选择模型` 的概要文本。
 */
function summarizeAgentModel(params: {
  agentId: string | null;
  modelName: string | null;
  availableModels: ModelEntryRecord[];
}): string {
  const { agentId, modelName, availableModels } = params;
  // Agent 折叠标签与 AgentSelector 同源（developer → Developer；其他 agent 默认回显 agent_id）
  const agentLabel = agentId === "developer" ? "Developer" : agentId;
  // Model 显示：null 时固定「选择模型」，否则用 display_name（与 ModelSelector currentLabel 同源）
  const modelLabel =
    modelName === null
      ? "选择模型"
      : availableModels.find((m) => m.model_name === modelName)?.display_name ?? modelName;
  return `${agentLabel} · ${modelLabel}`;
}

/**
 * 新建任务页面组件。
 *
 * 展示 TaskHeaderBar（顶部）+ 居中大标题 + 四象限快捷入口（右下角含 agent·model 概要）
 * + 底部输入区与工作区选择器。
 * 工作区选择器支持在已有工作区继续，或通过系统目录选择器新建/接入工作区。
 *
 * @param props - 组件属性。
 * @param props.onCreated - task 创建并切换为当前任务后的页面切换回调。
 * @returns 新建任务页面的 React 元素。
 *
 * @throws 不主动抛出异常；创建失败会写入 useTask.operation.error 或选择器错误提示。
 *
 * @sideeffect 发送首条消息时调用后端 task 创建接口、更新 task/turn store，并建立 turn SSE 连接。
 */
export function NewTaskPage({ onCreated }: NewTaskPageProps) {
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const setActiveWorkspace = useWorkspaceStore((s) => s.setActiveWorkspace);

  const activeWorkspace = workspaces.find((w) => w.workspace_id === activeWorkspaceId) ?? null;

  const [inputValue, setInputValue] = useState("");
  const { createTask, operation } = useTask();
  // 在组件顶层完成 store 订阅，参数化传给纯函数 summarizeAgentModel；
  // 这样 helper 不会触发 React Hooks 规则误判（命名也不再以 use 开头）。
  const selectedAgentId = useTaskStore((s) => s.selectedAgentId);
  const selectedModelName = useTaskStore((s) => s.selectedModelName);
  const availableModels = useTaskStore((s) => s.availableModels);
  const agentModelSummary = useMemo(
    () =>
      summarizeAgentModel({
        agentId: selectedAgentId,
        modelName: selectedModelName,
        availableModels,
      }),
    [selectedAgentId, selectedModelName, availableModels],
  );

  // 工作区菜单状态
  const [menuOpen, setMenuOpen] = useState(false);
  // 目录选择进行中标记，用于禁用菜单项并展示 loading。
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  // 目录选择 / 创建工作区失败提示，展示在选择器下方。
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  // 发送前模型校验拦截提示（guardSend 拦截原因），展示在输入区下方。
  const [guardMessage, setGuardMessage] = useState<string | null>(null);
  // 厂商配置中心对话框开关：由 guardSend 拦截（openSettings=true）联动打开，受控传给 TaskHeaderBar。
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { guardSend } = useModelSendGuard();
  const menuRef = useRef<HTMLDivElement>(null);

  /**
   * 发送前模型校验拦截。
   *
   * 模型必须显式选择（无 Auto 语义）：guardSend 返回 ok=false 时展示拦截原因，
   * 并按 openSettings 标记联动打开厂商配置中心（未配置模型 / 缺 API Key 场景）。
   *
   * @returns 是否放行发送。
   */
  const runModelGuard = async (): Promise<boolean> => {
    const guard = await guardSend();
    if (!guard.ok) {
      setGuardMessage(guard.block.message);
      if (guard.block.openSettings) {
        setSettingsOpen(true);
      }
      // 任务创建被模型校验拦截是关键拒绝路径：记录拦截原因与上下文，
      // 便于排查「为什么创建不了任务、被哪条规则拦的」（可排查日志规范）。
      logWarn("任务创建被模型校验拦截", {
        module: "NewTaskPage",
        reason: guard.block.reason,
        message: guard.block.message,
        openSettings: guard.block.openSettings,
        workspace: activeWorkspaceId ?? null,
      });
      return false;
    }
    setGuardMessage(null);
    return true;
  };

  /**
   * 发送首条消息并创建任务。
   *
   * 当前已选工作区必须非空；模型未显式选择时被 guardSend 拦截并提示。
   */
  const handleCreate = async () => {
    const text = inputValue.trim();
    if (!text || !activeWorkspaceId || operation.loading) {
      return;
    }
    if (!(await runModelGuard())) {
      return;
    }
    const succeeded = await createTask(text, activeWorkspaceId);
    if (succeeded) {
      setInputValue("");
      onCreated();
    }
  };

  /** 点击快捷入口卡片：填充输入并直接创建任务（同样先过模型校验拦截）。 */
  const handleSuggestion = async (label: string) => {
    if (!activeWorkspaceId || operation.loading) {
      return;
    }
    if (!(await runModelGuard())) {
      return;
    }
    setInputValue(label);
    const succeeded = await createTask(label, activeWorkspaceId);
    if (succeeded) {
      setInputValue("");
      onCreated();
    }
  };

  /**
   * 弹出系统目录选择器并将所选目录注册为工作区。
   *
   * 复用 services/workspace 的共享流程；取消选择不做任何处理；
   * 选择失败在选择器下方回显错误而非静默丢弃。
   */
  const handlePickWorkspace = async () => {
    if (workspaceLoading) {
      return;
    }
    setWorkspaceLoading(true);
    setWorkspaceError(null);
    try {
      const workspace = await pickAndCreateWorkspace();
      if (!workspace) {
        return;
      }
      setMenuOpen(false);
    } catch (err) {
      logError("选择目录作为工作区失败", err, { module: "NewTaskPage" });
      setWorkspaceError(err instanceof Error ? err.message : "打开目录选择器失败");
    } finally {
      setWorkspaceLoading(false);
    }
  };

  // 用户切换模型（顶部选择器变更 selectedModelName）后清除拦截提示：
  // 提示「请先选择模型」等已失去意义，避免误导（2026-08-18 无 Auto 语义）。
  useEffect(() => {
    setGuardMessage(null);
  }, [selectedModelName]);

  // 点击外部关闭工作区菜单。
  useEffect(() => {
    const handler = (event: MouseEvent) => {
      if (!menuRef.current || menuRef.current.contains(event.target as Node)) {
        return;
      }
      setMenuOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <main className="flex h-full w-full min-w-0 flex-col overflow-hidden bg-background">
      {/* 顶部：task 维度元数据选择条（与 Chat 视图共享同一选择条；配置中心开关受控，供 guardSend 联动） */}
      <div className="flex w-full justify-end px-4 pt-4">
        <TaskHeaderBar settingsOpen={settingsOpen} onSettingsOpenChange={setSettingsOpen} />
      </div>

      <div className="mx-auto flex w-full max-w-content flex-1 flex-col items-center justify-center gap-8 px-4 py-12">
        {/* 居中标题：text-xl（轻微降权，让 TaskHeaderBar 不抢视觉重心） */}
        <h1 className="text-center text-xl font-semibold text-foreground">
          我们应该在 {activeWorkspace ? `「${activeWorkspace.name}」` : "当前工作区"} 中构建什么？
        </h1>

        {/* 四象限快捷入口卡片：右下角显示当前 agent·model 概要 */}
        <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
          {SUGGESTIONS.map(({ icon: Icon, label }) => (
            <button
              key={label}
              onClick={() => void handleSuggestion(label)}
              disabled={!activeWorkspaceId || operation.loading}
              className={cn(
                "flex flex-col items-start gap-2 rounded-md border border-border bg-background p-4 text-left transition-colors",
                activeWorkspaceId && !operation.loading
                  ? "hover:bg-accent/50 hover:text-accent-foreground"
                  : "cursor-not-allowed opacity-50",
              )}
            >
              <div className="flex w-full items-start justify-between gap-2">
                <Icon className="h-5 w-5 text-muted-foreground" />
                {/* 右下角 agent·model 概要：让用户感知「卡片会用此配置创建」 */}
                {/* eslint-disable-next-line tailwind/no-arbitrary-value */}
                <span data-testid="card-agent-model" className="text-[10px] text-muted-foreground">
                  {agentModelSummary}
                </span>
              </div>
              <span className="text-sm font-medium">{label}</span>
            </button>
          ))}
        </div>

        {/* 底部输入区 + 工作区选择器 */}
        <div className="w-full space-y-3">
          <div className="flex gap-2">
            <Input
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={(event) => {
                // IME 组合输入（中文/日文选词上屏）期间的 Enter 不触发创建。
                if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void handleCreate();
                }
              }}
              placeholder="描述这次任务..."
              className="h-12"
            />
            <Button
              variant="primary"
              onClick={handleCreate}
              disabled={!inputValue.trim() || !activeWorkspaceId || operation.loading}
              size="icon"
              className="h-12 w-12 shrink-0"
            >
              <Send className="h-4 w-4" />
            </Button>
          </div>

          {guardMessage && <p className="text-sm text-destructive">{guardMessage}</p>}
          {operation.error && <p className="text-sm text-destructive">{operation.error}</p>}

          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setMenuOpen((open) => !open)}
              className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent/50 hover:text-accent-foreground"
            >
              <Folder className="h-3.5 w-3.5" />
              {/* 16rem 为工作区名展示宽度上限，非通用语义，待收敛到 token */}
              {/* eslint-disable-next-line tailwind/no-arbitrary-value */}
              <span className="max-w-[16rem] truncate">{activeWorkspace?.name ?? "选择工作区"}</span>
              {menuOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </button>

            {workspaceError && <p className="mt-1 text-xs text-destructive">{workspaceError}</p>}

            {menuOpen && (
              <div className="absolute left-0 top-full z-40 mt-1 w-72 rounded-md border border-border bg-background p-1">
                <div className="px-2 py-1.5 text-xs font-medium text-muted-foreground">已有工作区</div>
                {workspaces.length === 0 && (
                  <div className="px-2 py-2 text-xs text-muted-foreground">暂无工作区</div>
                )}
                {workspaces.map((workspace) => {
                  const selected = workspace.workspace_id === activeWorkspaceId;
                  return (
                    <button
                      key={workspace.workspace_id}
                      onClick={() => {
                        setActiveWorkspace(workspace.workspace_id);
                        setMenuOpen(false);
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors",
                        selected ? "bg-accent text-accent-foreground" : "hover:bg-accent/50",
                      )}
                    >
                      <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <span className="flex-1 truncate">{workspace.name}</span>
                      {selected && <Check className="h-3.5 w-3.5 text-primary" />}
                    </button>
                  );
                })}
                <div className="my-1 border-t border-border" />
                <button
                  onClick={() => void handlePickWorkspace()}
                  disabled={workspaceLoading}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors hover:bg-accent/50 disabled:opacity-50"
                >
                  <Plus className="h-4 w-4 shrink-0" />
                  {workspaceLoading ? "选择目录中…" : "新建空白项目（选择目录）"}
                </button>
                <button
                  onClick={() => void handlePickWorkspace()}
                  disabled={workspaceLoading}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors hover:bg-accent/50 disabled:opacity-50"
                >
                  <FolderOpen className="h-4 w-4 shrink-0" />
                  使用现有文件夹（选择目录）
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
