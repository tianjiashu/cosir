"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FolderIcon,
  Loader2Icon,
  MessageSquarePlusIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  RefreshCwIcon,
} from "lucide-react";

import { Assistant } from "@/app/assistant";
import { Button } from "@/components/ui/button";
import { NewConversation } from "@/components/new-conversation";
import { DeleteConfirmDialog } from "@/components/delete-confirm-dialog";
import { ResourceActionMenu } from "@/components/resource-action-menu";
import { BackendStatusBanner } from "@/components/backend-status-banner";
import {
  deleteTask,
  deleteWorkspace,
  getWorkspaceTasks,
  getWorkspaces,
  type Workspace,
  type WorkspaceTask,
} from "@/lib/api/workspaces";

type WorkspaceWithTasks = Workspace & { tasks: WorkspaceTask[] };
type DeleteTarget =
  | { kind: "workspace"; id: number; label: string; taskCount: number }
  | { kind: "task"; id: number; label: string; taskCount: number }
  | null;

export function WorkspaceShell({ initialTaskId = null }: { initialTaskId?: number | null }) {
  const [collapsed, setCollapsed] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<number | null>(null);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<number | null>(null);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<number[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceWithTasks[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const initialTaskAppliedRef = useRef(false);
  const loadGenerationRef = useRef(0);

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    setStatus("loading");
    setError(null);
    try {
      const baseWorkspaces = await getWorkspaces();
      const withTasks = await Promise.all(baseWorkspaces.map(async (workspace) => {
        try {
          return { ...workspace, tasks: await getWorkspaceTasks(workspace.workspace_id) };
        } catch {
          return { ...workspace, tasks: [] };
        }
      }));
      if (generation !== loadGenerationRef.current) return;
      setWorkspaces(withTasks);
      const initialWorkspace = initialTaskId === null ? undefined : withTasks.find((workspace) => workspace.tasks.some((task) => task.task_id === initialTaskId));
      const shouldApplyInitialTask = initialTaskId !== null && initialWorkspace !== undefined && !initialTaskAppliedRef.current;
      if (shouldApplyInitialTask && initialWorkspace) {
        setActiveTaskId(initialTaskId);
        setSelectedWorkspaceId(initialWorkspace.workspace_id);
        initialTaskAppliedRef.current = true;
      }
      setSelectedWorkspaceId((current) => shouldApplyInitialTask && initialWorkspace ? initialWorkspace.workspace_id : (current && withTasks.some((workspace) => workspace.workspace_id === current) ? current : withTasks[0]?.workspace_id ?? null));
      setExpandedWorkspaceIds((current) => current.length > 0 ? current.filter((id) => withTasks.some((workspace) => workspace.workspace_id === id)) : withTasks.map((workspace) => workspace.workspace_id));
      setStatus("ready");
    } catch (cause) {
      if (generation !== loadGenerationRef.current) return;
      setStatus("error");
      setError(cause instanceof Error ? cause.message : "工作区加载失败，请重试");
    }
  }, [initialTaskId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      // `WorkspaceShell` 可能在任务路由变化时被 Next.js 复用。路由传入的任务
      // 身份优先于旧的本地选择，先切换 activeTaskId，再加载列表补齐工作区信息。
      // 放在异步调度中可避免 effect 主体同步触发级联渲染。
      setActiveTaskId(initialTaskId);
      void load();
    }, 0);
    return () => {
      window.clearTimeout(timer);
      // 使路由切换或卸载时仍在进行的旧请求失效，避免其结果覆盖新任务。
      loadGenerationRef.current += 1;
    };
  }, [initialTaskId, load]);

  // `WorkspaceShell` 可能在任务路由变化时被 Next.js 复用。路由传入的任务身份
  // 是当前页面的权威来源，必须在下一轮加载开始时同步到 activeTaskId，避免 A → B
  // 时旧对话在工作区列表加载期间继续显示，或因列表请求异常而永久残留。与此同时
  // 重置应用标记，让 load() 完成后补齐 B 所属工作区；同一任务内手动刷新时
  // initialTaskId 不变，因此不会抢回用户当前选择的任务。
  useEffect(() => {
    initialTaskAppliedRef.current = false;
  }, [initialTaskId]);

  useEffect(() => {
    if (activeTaskId) window.history.replaceState({}, "", `/tasks/${activeTaskId}`);
  }, [activeTaskId]);

  const selectedWorkspace = useMemo(() => workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId), [selectedWorkspaceId, workspaces]);
  const toggleWorkspace = (workspaceId: number) => setExpandedWorkspaceIds((current) => current.includes(workspaceId) ? current.filter((id) => id !== workspaceId) : [...current, workspaceId]);
  const startNewConversation = (workspaceId = selectedWorkspaceId) => { setSelectedWorkspaceId(workspaceId); setActiveTaskId(null); window.history.pushState({}, "", "/"); };
  const requestDelete = (target: NonNullable<DeleteTarget>) => { setDeleteError(null); setDeleteTarget(target); };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      if (deleteTarget.kind === "workspace") {
        const deletingCurrentWorkspace = selectedWorkspaceId === deleteTarget.id || workspaces.some((workspace) => workspace.workspace_id === deleteTarget.id && workspace.tasks.some((task) => task.task_id === activeTaskId));
        await deleteWorkspace(deleteTarget.id);
        if (deletingCurrentWorkspace) {
          setSelectedWorkspaceId(null);
          setActiveTaskId(null);
          window.history.pushState({}, "", "/");
        }
      } else {
        await deleteTask(deleteTarget.id);
        if (activeTaskId === deleteTarget.id) {
          setActiveTaskId(null);
          window.history.pushState({}, "", "/");
        }
      }
      setDeleteTarget(null);
      await load();
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "删除失败，请重试");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="bg-background flex h-dvh min-h-0">
      <BackendStatusBanner />
      <aside className={`bg-muted/20 flex shrink-0 flex-col border-r transition-[width] duration-200 ${collapsed ? "w-14" : "w-72"}`}>
        <div className="flex h-14 items-center justify-between border-b px-3">
          {!collapsed && <span className="text-sm font-semibold">工作区</span>}
          <Button variant="ghost" size="icon-sm" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "展开侧栏" : "收起侧栏"}>
            {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
          </Button>
        </div>
        {!collapsed && (
          <>
            <div className="flex gap-2 p-3">
              <Button className="flex-1 justify-start gap-2" onClick={() => startNewConversation()}><MessageSquarePlusIcon className="size-4" />新对话</Button>
              <Button variant="outline" size="icon" onClick={() => void load()} disabled={status === "loading"} aria-label="刷新工作区"><RefreshCwIcon className={status === "loading" ? "animate-spin" : ""} /></Button>
            </div>
            <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-4" aria-label="工作区任务列表">
              <p className="text-muted-foreground px-2 pb-2 text-xs font-medium">我的工作区</p>
              {status === "loading" && <Loader2Icon className="text-muted-foreground mx-2 size-4 animate-spin" />}
              {status === "error" && <div className="px-2 text-xs"><p className="text-destructive mb-2">{error}</p><Button variant="outline" size="sm" onClick={() => void load()}>重试</Button></div>}
              {status === "ready" && workspaces.length === 0 && <p className="text-muted-foreground px-2 text-xs">暂无工作区</p>}
              <div className="space-y-1">
                {workspaces.map((workspace) => {
                  const expanded = expandedWorkspaceIds.includes(workspace.workspace_id);
                  return (
                    <section key={workspace.workspace_id}>
                      <div className={`group flex items-center rounded-md ${selectedWorkspaceId === workspace.workspace_id && !activeTaskId ? "bg-muted" : "hover:bg-muted"}`}>
                        <button type="button" className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left text-sm" onClick={() => toggleWorkspace(workspace.workspace_id)}>
                          {expanded ? <ChevronDownIcon className="size-3.5 shrink-0" /> : <ChevronRightIcon className="size-3.5 shrink-0" />}
                          <FolderIcon className="text-muted-foreground size-4 shrink-0" /><span className="truncate">{workspace.name}</span><span className="text-muted-foreground ml-auto text-xs">{workspace.tasks.length}</span>
                        </button>
                        <ResourceActionMenu label={workspace.name} onDelete={() => requestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, taskCount: workspace.tasks.length })} />
                      </div>
                      {expanded && <div className="ml-5 space-y-0.5 border-l pl-2">
                        {workspace.tasks.map((task) => <div className="group flex items-center" key={task.task_id}>
                          <button type="button" className={`hover:bg-muted flex min-w-0 flex-1 items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-xs ${activeTaskId === task.task_id ? "bg-muted font-medium" : ""}`} onClick={() => { setSelectedWorkspaceId(workspace.workspace_id); setActiveTaskId(task.task_id); }}>
                            <span className="truncate">{task.title}</span><span className="text-muted-foreground shrink-0">{new Date(task.updated_at).toLocaleDateString()}</span>
                          </button>
                          <ResourceActionMenu label={task.title} onDelete={() => requestDelete({ kind: "task", id: task.task_id, label: task.title, taskCount: 0 })} />
                        </div>)}
                      </div>}
                    </section>
                  );
                })}
              </div>
            </nav>
          </>
        )}
      </aside>
      <main className="min-w-0 flex-1">
        <div className="flex h-14 items-center justify-between border-b px-5"><div className="min-w-0"><p className="text-sm font-medium">{activeTaskId ? "对话" : "新对话"}</p><p className="text-muted-foreground truncate text-xs">{activeTaskId ? "已存在任务" : "选择工作区后开始创建对话"}</p></div>{selectedWorkspace && <div className="text-muted-foreground flex items-center gap-2 text-xs"><FolderIcon className="size-3.5" />{selectedWorkspace.name}</div>}</div>
        {activeTaskId ? <div className="h-[calc(100dvh-3.5rem)]"><Assistant taskId={activeTaskId} /></div> : <div className="h-[calc(100dvh-3.5rem)]"><NewConversation workspaces={workspaces} selectedWorkspaceId={selectedWorkspaceId} onWorkspaceChange={setSelectedWorkspaceId} onWorkspaceCreated={load} /></div>}
      </main>
      {deleteTarget && <DeleteConfirmDialog open title={deleteTarget.kind === "workspace" ? `删除工作区“${deleteTarget.label}”？` : `删除任务“${deleteTarget.label}”？`} description={deleteTarget.kind === "workspace" ? `此操作将永久删除该工作区及其下的 ${deleteTarget.taskCount} 个任务和全部对话数据。` : "此操作将永久删除该任务及其全部对话数据，不影响所属工作区和其他任务。"} warning="删除后无法撤销。" error={deleteError} busy={deleting} onOpenChange={(open) => { if (!open) { setDeleteTarget(null); setDeleteError(null); } }} onConfirm={() => void confirmDelete()} />}
    </div>
  );
}
