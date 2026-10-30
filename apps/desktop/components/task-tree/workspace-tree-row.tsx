import { createContext, useContext, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, FolderIcon, GitForkIcon, PinIcon, Settings2Icon } from "lucide-react";
import type { NodeApi, NodeRendererProps, RowRendererProps } from "react-arborist";

import { Button } from "@/components/ui/button";
import { formatRelativeTime } from "@/lib/format/relative-time";
import { cn } from "@/lib/utils";
import { TaskRunStatusDot } from "./task-run-status";
import type { TaskSidebarNode, WorkspaceSidebarNode, WorkspaceTreeSource } from "./workspace-tree-model";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";

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
      aria-label={sidebarNodeLabel(item)}
      aria-selected={current}
      aria-current={current ? (item.kind === "task" ? "page" : "true") : undefined}
      data-current={current || undefined}
      onFocus={(event) => event.stopPropagation()}
      onClick={node.handleClick}
      onKeyDown={(event) => {
        // 行内按钮保留原生键盘行为，不让树拦截 Tab、空格或方向键。
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

function sidebarNodeLabel(item: WorkspaceSidebarNode): string {
  switch (item.kind) {
    case "task": return item.task.full_title;
    case "workspace": return item.workspace.name;
    case "pinned": return PINNED_GROUP_LABEL;
    case "placeholder": return item.label;
  }
}

const PINNED_GROUP_LABEL = "置顶";

type RowActions = {
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderTaskActions: (item: TaskSidebarNode, tabIndex: number) => ReactNode;
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
  if (item.kind === "pinned") {
    return (
      <div style={style} className="workspace-tree-node">
        <button
          type="button"
          tabIndex={tabIndex}
          className="focus-ring flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:text-foreground"
          aria-expanded={node.isOpen}
          aria-label={`${node.isOpen ? "收起" : "展开"}置顶任务`}
          onClick={(event) => { event.stopPropagation(); node.toggle(); }}
        >
          {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
        </button>
        <span className="flex min-w-0 flex-1 items-center gap-2 py-2 text-sm">
          <PinIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="workspace-tree-title truncate">{PINNED_GROUP_LABEL}</span>
          <span className="shrink-0 text-xs font-normal tabular-nums text-muted-foreground" aria-label={`${item.children.length} 个置顶任务`}>{item.children.length}</span>
        </span>
      </div>
    );
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
  return <WorkspaceTaskRow node={node} style={style} item={item} actions={renderTaskActions(item, tabIndex)} />;
}

/** workspace 在第 0 层、顶层任务在第 1 层；更深的都是挂在源任务下的 Fork。 */
const ROOT_TASK_LEVEL = 1;
/**
 * 顶层任务不随 workspace 层缩进，状态标记列对齐 workspace 的展开按钮；
 * Fork 每深一层只缩 12px，避免多层后标题被挤窄。
 */
const ROOT_TASK_INDENT = 0;
const FORK_INDENT = 12;
/** 状态标记列宽：只容纳状态点或 Fork 图标；引导线位置据此推算，宽度只在这里定义。 */
const TASK_MARKER_WIDTH = 20;
/** 标记与标题的间距，与 `.workspace-tree-task` 的 `gap` 一致。 */
const TASK_MARKER_GAP = 8;

type BranchGuide = "pass" | "none" | "tee" | "end";

/**
 * 计算 Fork 行左侧的树形引导线：每层祖先占一列，最后一列是连到父任务的 ├ / └。
 *
 * 祖先那一列是否继续往下画，取决于路径上低一层的祖先后面还有没有兄弟；
 * 只读 react-arborist 的可见节点关系，模型里不另存深度或「是否末子」。
 */
function branchGuides(node: NodeApi<WorkspaceSidebarNode>): BranchGuide[] {
  const guides: BranchGuide[] = [node.nextSibling ? "tee" : "end"];
  for (let ancestor = node.parent; ancestor && ancestor.level > ROOT_TASK_LEVEL; ancestor = ancestor.parent) {
    guides.unshift(ancestor.nextSibling ? "pass" : "none");
  }
  return guides;
}

function WorkspaceTaskRow({ node, style, item, actions }: Pick<NodeRendererProps<WorkspaceSidebarNode>, "node" | "style"> & { item: TaskSidebarNode; actions: ReactNode }) {
  const task = item.task;
  const status = useTaskExecutionStatus(task.task_id);
  const nested = node.level > ROOT_TASK_LEVEL;
  // 嵌套行的 Fork 身份由层级表达；源任务已不在列表中、被提为顶层的 Fork 仍保留图标。
  const showForkIcon = task.task_type === "fork" && !nested;
  // 覆盖 react-arborist 按层级等距算出的 paddingLeft，只收紧 Fork 层。
  const paddingLeft = ROOT_TASK_INDENT + (node.level - ROOT_TASK_LEVEL) * FORK_INDENT;
  return (
    <div style={{ ...style, paddingLeft }} className="workspace-tree-node workspace-tree-task">
      {nested && branchGuides(node).map((guide, index) => (
        // 第 i 列落在对应祖先标题起点左侧 4px；横线 8px 恰好到下一层标记右缘，状态点压在线尾。
        <span key={index} className="workspace-tree-guide" data-guide={guide} style={{ left: ROOT_TASK_INDENT + index * FORK_INDENT + TASK_MARKER_WIDTH + TASK_MARKER_GAP - 4 }} aria-hidden="true" />
      ))}
      <span className="workspace-tree-task-marker" style={{ flexBasis: TASK_MARKER_WIDTH }} data-nested={nested || undefined}>
        {showForkIcon && <GitForkIcon className="size-3.5 text-muted-foreground" aria-label="Fork 任务" />}
        <TaskRunStatusDot status={status} className={showForkIcon ? "absolute -top-0.5 right-0 size-1.5" : undefined} />
      </span>
      <span className="flex min-w-0 flex-1 items-center gap-1">
        <span className="workspace-tree-title min-w-0 truncate" title={item.workspaceName ? `${task.full_title}\n工作区：${item.workspaceName}` : task.full_title}>{task.full_title}</span>
        {node.isInternal && (
          // 不进 Tab 序列：键盘用户以左右方向键展开 / 收起，与 workspace 行一致地交给 react-arborist。
          <button
            type="button"
            tabIndex={-1}
            className="flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground"
            aria-expanded={node.isOpen}
            aria-label={`${node.isOpen ? "收起" : "展开"} ${task.full_title} 的 Fork`}
            onClick={(event) => { event.stopPropagation(); node.toggle(); }}
          >
            {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
          </button>
        )}
      </span>
      {/* 元信息与操作叠在同一尾栏格内，宽度取较宽者；操作显示时隐藏时间，标题不跳动。 */}
      <div className="workspace-tree-task-tail">
        <time className="workspace-tree-time whitespace-nowrap text-2xs tabular-nums text-muted-foreground" dateTime={task.updated_at} title={task.updated_at}>{formatRelativeTime(task.updated_at)}</time>
        <div className="workspace-tree-actions" onClick={(event) => event.stopPropagation()}>{actions}</div>
      </div>
    </div>
  );
}
