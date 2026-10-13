import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, FolderIcon, GitForkIcon, MessageSquareIcon, Settings2Icon } from "lucide-react";
import { getTreeLinePrefix, Tree, type NodeApi, type NodeRendererProps, type TreeApi } from "react-arborist";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { Button } from "@/components/ui/button";
import { cn } from "cn";
import { buildWorkspaceTreeModel, taskTreeNodeId, workspaceTreeNodeId, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "./workspace-tree-model";
import type { TaskTreeNode } from "./task-tree-model";

type WorkspaceTreeProps = {
  workspaces: WorkspaceTreeSource[];
  activeTaskId: number | null;
  selectedWorkspaceId: number | null;
  expandedWorkspaceIds: number[] | null;
  onWorkspaceExpansionChange: (workspaceId: number, expanded: boolean) => void;
  onSelectWorkspace: (workspaceId: number) => void;
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  renderWorkspaceActions: (workspace: WorkspaceTreeSource) => ReactNode;
  renderTaskActions: (task: TaskTreeNode) => ReactNode;
};

const ROW_HEIGHT = 38;

function WorkspaceTreeRow({
  style,
  node,
  selectedWorkspaceId,
  activeTaskId,
  onSelectWorkspace,
  onOpenWorkspaceSettings,
  renderWorkspaceActions,
  renderTaskActions,
}: NodeRendererProps<WorkspaceSidebarNode> & Pick<WorkspaceTreeProps,
  "selectedWorkspaceId" | "activeTaskId" | "onSelectWorkspace" | "onOpenWorkspaceSettings" | "renderWorkspaceActions" | "renderTaskActions"
>) {
  const item = node.data;
  if (item.kind === "placeholder") {
    return <div style={style} className="flex items-center pl-16 text-xs text-muted-foreground">{item.label}</div>;
  }

  if (item.kind === "workspace") {
    const workspace = item.workspace;
    const isSelected = selectedWorkspaceId === workspace.workspace_id && activeTaskId === null;
    const toggle = (event: MouseEvent) => {
      event.stopPropagation();
      node.toggle();
    };
    return (
      <div
        style={style}
        className={cn(
          "group flex min-w-0 items-center rounded-md pr-1 text-sm transition-colors",
          isSelected ? "bg-muted" : "hover:bg-muted",
          node.isFocused && !isSelected && "ring-1 ring-primary/15",
        )}
      >
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left"
          onClick={(event) => {
            toggle(event);
            onSelectWorkspace(workspace.workspace_id);
          }}
          aria-expanded={node.isOpen}
          aria-label={`${node.isOpen ? "收起" : "展开"}工作区 ${workspace.name}`}
        >
          {node.isOpen ? <ChevronDownIcon className="size-3.5 shrink-0" /> : <ChevronRightIcon className="size-3.5 shrink-0" />}
          <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate">{workspace.name}</span>
          {workspace.taskLoadError
            ? <span className="ml-auto shrink-0 text-xs text-destructive" title={workspace.taskLoadError}>加载失败</span>
            : <span className="ml-auto shrink-0 text-xs text-muted-foreground">{workspace.tasks.length}</span>}
        </button>
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0 text-muted-foreground"
          onClick={(event) => {
            event.stopPropagation();
            onOpenWorkspaceSettings(workspace.workspace_id);
          }}
          aria-label={`工作区配置：${workspace.name}`}
          title={`配置 ${workspace.name}`}
        >
          <Settings2Icon className="size-4" />
        </Button>
        <div className="shrink-0" onClick={(event) => event.stopPropagation()}>
          {renderWorkspaceActions(workspace)}
        </div>
      </div>
    );
  }

  const task = item.task;
  const hasChildren = node.isInternal;
  return (
    <div
      style={style}
      className={cn(
        "group relative flex min-w-0 items-center rounded-lg pr-1 text-xs transition-colors",
        node.isSelected && "bg-primary/10 text-primary ring-1 ring-primary/20",
        node.isFocused && !node.isSelected && "bg-muted/80 ring-1 ring-primary/15",
        !node.isSelected && !node.isFocused && "hover:bg-muted/70",
      )}
    >
      {hasChildren && <button
        type="button"
        tabIndex={-1}
        className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:text-foreground"
        aria-label={node.isOpen ? `收起${task.full_title}` : `展开${task.full_title}`}
        onClick={(event) => {
          event.stopPropagation();
          node.toggle();
        }}
      >
        {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
      </button>}
      <span className="w-auto shrink-0 whitespace-pre font-mono text-[11px] text-muted-foreground/50" aria-hidden="true">
        {getTreeLinePrefix(node, { last: "└─ ", middle: "├─ ", pipe: "│  ", blank: "   " })}
      </span>
      <div className="flex min-w-0 flex-1 items-center gap-2" title={task.full_title}>
        {task.task_type === "fork"
          ? <GitForkIcon className="size-3.5 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden="true" />
          : <MessageSquareIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />}
        <span className="min-w-0 flex-1 line-clamp-2 break-words font-medium leading-4">{task.display_title}</span>
      </div>
      <div className="flex size-7 shrink-0 items-center justify-center rounded-md opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100" onClick={(event) => event.stopPropagation()}>
        {renderTaskActions(task)}
      </div>
    </div>
  );
}

export function WorkspaceTree({
  workspaces,
  activeTaskId,
  selectedWorkspaceId,
  expandedWorkspaceIds,
  onWorkspaceExpansionChange,
  onSelectWorkspace,
  onOpenWorkspaceSettings,
  onSelectTask,
  renderWorkspaceActions,
  renderTaskActions,
}: WorkspaceTreeProps) {
  const model = useMemo(() => buildWorkspaceTreeModel(workspaces), [workspaces]);
  const treeRef = useRef<TreeApi<WorkspaceSidebarNode> | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const [treeHeight, setTreeHeight] = useState(0);

  const syncTreeHeight = useCallback(() => {
    const container = containerRef.current;
    if (container) setTreeHeight(Math.floor(container.getBoundingClientRect().height));
  }, []);

  useLayoutEffect(() => {
    syncTreeHeight();
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(syncTreeHeight);
    observer.observe(container);
    return () => observer.disconnect();
  }, [syncTreeHeight]);

  const initialOpenState = useMemo(() => {
    if (expandedWorkspaceIds === null) return undefined;
    return Object.fromEntries(workspaces.map((workspace) => [
      workspaceTreeNodeId(workspace.workspace_id),
      expandedWorkspaceIds.includes(workspace.workspace_id),
    ]));
  }, [expandedWorkspaceIds, workspaces]);

  useLayoutEffect(() => {
    if (expandedWorkspaceIds === null) return;
    for (const workspace of workspaces) {
      const id = workspaceTreeNodeId(workspace.workspace_id);
      const shouldOpen = expandedWorkspaceIds.includes(workspace.workspace_id);
      if (shouldOpen && !treeRef.current?.isOpen(id)) treeRef.current?.open(id);
      else if (!shouldOpen && treeRef.current?.isOpen(id)) treeRef.current?.close(id);
    }
  }, [expandedWorkspaceIds, model, workspaces]);

  useEffect(() => {
    if (activeTaskId === null) return;
    treeRef.current?.openParents(taskTreeNodeId(activeTaskId));
  }, [activeTaskId, model]);

  const handleToggle = (id: string) => {
    const workspace = workspaces.find((candidate) => workspaceTreeNodeId(candidate.workspace_id) === id);
    if (!workspace) return;
    onWorkspaceExpansionChange(workspace.workspace_id, treeRef.current?.isOpen(id) ?? false);
  };

  return (
    <div ref={containerRef} className="h-full min-h-0 min-w-0">
      {treeHeight > 0 && model.length > 0 && (
        <Tree<WorkspaceSidebarNode>
          ref={treeRef}
          data={model}
          idAccessor={(item) => item.id}
          childrenAccessor={(item) => item.children.length > 0 ? item.children : null}
          width="100%"
          height={treeHeight}
          indent={16}
          rowHeight={ROW_HEIGHT}
          overscanCount={8}
          openByDefault
          initialOpenState={initialOpenState}
          selection={activeTaskId === null ? undefined : taskTreeNodeId(activeTaskId)}
          disableMultiSelection
          disableDeselectOnClick
          disableDrag
          disableDrop
          disableEdit
          aria-label="工作区和任务列表"
          onToggle={handleToggle}
          onActivate={(node: NodeApi<WorkspaceSidebarNode>) => {
            if (node.data.kind === "task") onSelectTask(node.data.task, node.data.task.workspace_id);
          }}
        >
          {(props) => <WorkspaceTreeRow {...props} selectedWorkspaceId={selectedWorkspaceId} activeTaskId={activeTaskId} onSelectWorkspace={onSelectWorkspace} onOpenWorkspaceSettings={onOpenWorkspaceSettings} renderWorkspaceActions={renderWorkspaceActions} renderTaskActions={renderTaskActions} />}
        </Tree>
      )}
    </div>
  );
}
