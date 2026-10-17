import { createContext, useContext, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, FolderIcon, GitForkIcon, Settings2Icon } from "lucide-react";
import type { NodeRendererProps, RowRendererProps } from "react-arborist";

import { Button } from "@/components/ui/button";
import { formatRelativeTime } from "@/lib/format/relative-time";
import { cn } from "@/lib/utils";
import type { TaskTreeNode } from "./task-tree-model";
import { TaskRunStatusDot } from "./task-run-status";
import type { WorkspaceSidebarNode, WorkspaceTreeSource } from "./workspace-tree-model";

/** 当前项由路由与工作区选择决定；树的键盘焦点不构成第二份导航状态。 */
export function WorkspaceTreeRow({ node, innerRef, attrs, children }: RowRendererProps<WorkspaceSidebarNode>) {
  const item = node.data;
  const current = node.id === node.tree.props.selection;
  return (
    <div
      {...attrs}
      ref={innerRef}
      style={{ ...attrs.style, minWidth: 0 }}
      className="workspace-tree-row"
      aria-label={item.kind === "task" ? item.task.full_title : item.kind === "workspace" ? item.workspace.name : item.label}
      aria-selected={current}
      aria-current={current ? (item.kind === "task" ? "page" : "true") : undefined}
      data-current={current || undefined}
      onFocus={(event) => event.stopPropagation()}
      onClick={node.handleClick}
      onKeyDown={(event) => {
        // 行内按钮与 Portal 菜单保留原生键盘行为，不让树拦截 Tab、空格或方向键。
        if (event.target !== event.currentTarget || event.key === "Tab") {
          event.stopPropagation();
        } else if (event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
          node.activate();
        }
      }}
    >
      {children}
    </div>
  );
}

type RowActions = {
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderTaskActions: (task: TaskTreeNode, tabIndex: number) => ReactNode;
};

// Tree 将 renderer 当组件挂载；通过上下文传操作，避免回调变化时重挂载并丢失焦点或菜单。
export const WorkspaceTreeActionsContext = createContext<RowActions | null>(null);

export function WorkspaceTreeNode({ node, style }: NodeRendererProps<WorkspaceSidebarNode>) {
  // 此 renderer 始终由 WorkspaceTree 的 Provider 装配。
  const { onOpenWorkspaceSettings, renderWorkspaceActions, renderTaskActions } = useContext(WorkspaceTreeActionsContext)!;
  const item = node.data;
  // 只让焦点行的操作进入 Tab 序列，方向键移动仍交给 react-arborist。
  const tabIndex = node.isFocused ? 0 : -1;
  if (item.kind === "placeholder") {
    return <div style={style} className={cn("workspace-tree-node", item.isError ? "text-destructive" : "text-muted-foreground")}><span className="truncate pl-7 text-xs">{item.label}</span></div>;
  }
  if (item.kind === "workspace") {
    const workspace = item.workspace;
    return (
      <div style={style} className="workspace-tree-node">
        <button
          type="button"
          tabIndex={tabIndex}
          className="focus-ring flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:text-foreground"
          aria-expanded={node.isOpen}
          aria-label={`${node.isOpen ? "收起" : "展开"}工作区 ${workspace.name}`}
          onClick={(event) => { event.stopPropagation(); node.toggle(); }}
        >
          {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
        </button>
        <button
          type="button"
          tabIndex={tabIndex}
          className="focus-ring flex min-w-0 flex-1 items-center gap-2 rounded-md py-2 text-left text-sm"
          aria-label={`工作区 ${workspace.name}`}
          title={workspace.name}
          onClick={(event) => { event.stopPropagation(); node.activate(); }}
        >
          <FolderIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="workspace-tree-title truncate">{workspace.name}</span>
          <span className="shrink-0 text-xs font-normal tabular-nums text-muted-foreground" aria-label={`${workspace.tasks.length} 个任务`}>{workspace.tasks.length}</span>
        </button>
        {workspace.taskLoadError && <span className="shrink-0 text-xs text-destructive" title={workspace.taskLoadError}>加载失败</span>}
        <div className="workspace-tree-workspace-actions workspace-tree-actions" onClick={(event) => event.stopPropagation()}>
          <Button variant="ghost" size="icon-sm" tabIndex={tabIndex} className="text-muted-foreground" aria-label={`工作区配置：${workspace.name}`} title={`配置 ${workspace.name}`} onClick={() => onOpenWorkspaceSettings(workspace.workspace_id)}>
            <Settings2Icon className="size-4" />
          </Button>
          {renderWorkspaceActions(workspace, tabIndex)}
        </div>
      </div>
    );
  }
  return <WorkspaceTaskRow style={style} task={item.task} actions={renderTaskActions(item.task, tabIndex)} />;
}

function WorkspaceTaskRow({ style, task, actions }: Pick<NodeRendererProps<WorkspaceSidebarNode>, "style"> & { task: TaskTreeNode; actions: ReactNode }) {
  return (
    <div style={style} className="workspace-tree-node workspace-tree-task">
      <span className="workspace-tree-task-marker">
        {task.task_type === "fork" && <GitForkIcon className="size-3.5 text-muted-foreground" aria-label="Fork 任务" />}
        <TaskRunStatusDot status={task.execution_status} className={task.task_type === "fork" ? "absolute -top-0.5 right-1 size-1.5" : undefined} />
      </span>
      <span className="workspace-tree-title min-w-0 flex-1 truncate" title={task.full_title}>{task.full_title}</span>
      {/* 元信息与操作共用固定尾栏；Portal 打开后由触发器状态保持可见，避免标题跳动。 */}
      <div className="workspace-tree-task-tail">
        <time className="workspace-tree-time truncate text-2xs tabular-nums text-muted-foreground" dateTime={task.updated_at} title={task.updated_at}>{formatRelativeTime(task.updated_at)}</time>
        <div className="workspace-tree-actions" onClick={(event) => event.stopPropagation()}>{actions}</div>
      </div>
    </div>
  );
}
