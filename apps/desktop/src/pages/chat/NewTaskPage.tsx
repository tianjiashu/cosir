/**
 * 新建任务页面。
 *
 * 负责选择 workspace、输入首条消息并触发 task + first turn 创建。
 *
 * 布局参考 Codex 风格：居中大标题 + 快捷入口卡片 + 底部输入区，
 * 工作区选择器放在输入区左下角，本质是选择本地目录。
 *
 * 工作区选择直接调用系统目录选择器，不再要求手动输入路径。
 *
 * @module pages/chat/NewTaskPage
 */

import { useState, useRef, useEffect } from "react";
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
import { logError } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { pickAndCreateWorkspace } from "@/services/workspace";

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
 * 新建任务页面组件。
 *
 * 展示居中大标题、四象限快捷入口、底部输入区与工作区选择器。
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

  // 工作区菜单状态
  const [menuOpen, setMenuOpen] = useState(false);
  // 目录选择进行中标记，用于禁用菜单项并展示 loading。
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  // 目录选择 / 创建工作区失败提示，展示在选择器下方。
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  /**
   * 发送首条消息并创建任务。
   *
   * 当前已选工作区必须非空。
   */
  const handleCreate = async () => {
    const text = inputValue.trim();
    if (!text || !activeWorkspaceId || operation.loading) {
      return;
    }
    const succeeded = await createTask(text, activeWorkspaceId);
    if (succeeded) {
      setInputValue("");
      onCreated();
    }
  };

  /** 点击快捷入口卡片：填充输入并直接创建任务。 */
  const handleSuggestion = async (label: string) => {
    if (!activeWorkspaceId || operation.loading) {
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
      <div className="mx-auto flex w-full max-w-content flex-1 flex-col items-center justify-center gap-8 px-4 py-12">
        {/* 居中大标题 */}
        <h1 className="text-center text-2xl font-semibold text-foreground">
          我们应该在 {activeWorkspace ? `「${activeWorkspace.name}」` : "当前工作区"} 中构建什么？
        </h1>

        {/* 四象限快捷入口卡片 */}
        <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
          {SUGGESTIONS.map(({ icon: Icon, label }) => (
            <button
              key={label}
              onClick={() => void handleSuggestion(label)}
              disabled={!activeWorkspaceId || operation.loading}
              className={cn(
                "flex flex-col items-start gap-2 rounded-lg border border-border bg-background p-4 text-left transition-colors",
                activeWorkspaceId && !operation.loading
                  ? "hover:bg-accent/50 hover:text-accent-foreground"
                  : "cursor-not-allowed opacity-50",
              )}
            >
              <Icon className="h-5 w-5 text-muted-foreground" />
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
                if (event.key === "Enter") {
                  event.preventDefault();
                  void handleCreate();
                }
              }}
              placeholder="描述这次任务..."
              className="h-12"
            />
            <Button
              onClick={handleCreate}
              disabled={!inputValue.trim() || !activeWorkspaceId || operation.loading}
              size="icon"
              className="h-12 w-12 shrink-0"
            >
              <Send className="h-4 w-4" />
            </Button>
          </div>

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
              <div className="absolute left-0 top-full z-40 mt-1 w-72 rounded-lg border border-border bg-background p-1 shadow-lg">
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
