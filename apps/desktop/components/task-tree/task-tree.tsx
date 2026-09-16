import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, GitForkIcon, MessageSquareIcon } from "lucide-react";
import { getTreeLinePrefix, Tree, type NodeRendererProps, type NodeApi, type TreeApi } from "react-arborist";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { cn } from "cn";
import { buildTaskTreeModel, type TaskTreeNode } from "./task-tree-model";

type TaskTreeProps = {
  tasks: WorkspaceTask[];
  activeTaskId: number | null;
  onSelectTask: (task: WorkspaceTask) => void;
  /** 操作菜单接收树节点：节点已带清洗后的展示标题，避免把含内部 token 的原文交给 UI。 */
  renderActions: (task: TaskTreeNode) => ReactNode;
};

const ROW_HEIGHT = 38;
const MAX_TREE_HEIGHT = 320;

function TaskNode({
  style,
  node,
  renderActions,
}: NodeRendererProps<TaskTreeNode> & Pick<TaskTreeProps, "renderActions">) {
  const task = node.data;
  const isFork = task.task_type === "fork";
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
      <button
        type="button"
        tabIndex={-1}
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-md",
          hasChildren ? "text-muted-foreground hover:text-foreground" : "invisible",
        )}
        aria-label={hasChildren ? (node.isOpen ? `收起${task.full_title}` : `展开${task.full_title}`) : undefined}
        onClick={(event) => {
          event.stopPropagation();
          if (hasChildren) node.toggle();
        }}
      >
        {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
      </button>
      <span className="text-muted-foreground/50 w-auto shrink-0 whitespace-pre font-mono text-[11px]" aria-hidden="true">
        {getTreeLinePrefix(node, { last: "└─ ", middle: "├─ ", pipe: "│  ", blank: "   " })}
      </span>
      {/* 右侧为操作按钮预留固定宽度（pr-9），标题再长也只会在自己的宽度内被省略号截断。 */}
      <div className="flex min-w-0 flex-1 items-center gap-2 py-1.5 pr-9" title={task.full_title}>
        {isFork ? <GitForkIcon className="text-sky-600 dark:text-sky-400 size-3.5 shrink-0" aria-hidden="true" /> : <MessageSquareIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />}
        <span className="min-w-0 flex-1 truncate font-medium">{task.display_title}</span>
      </div>
      {/* 操作区脱离常规流：即使标题异常变长，按钮也固定在行尾预留区内，不会被挤出可视区域。 */}
      <div
        className="bg-background/95 absolute right-1 top-1/2 flex -translate-y-1/2 items-center rounded-md backdrop-blur-sm opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
        onClick={(event) => event.stopPropagation()}
      >
        {renderActions(task)}
      </div>
    </div>
  );
}

function activateTask(node: NodeApi<TaskTreeNode>, onSelectTask: (task: WorkspaceTask) => void) {
  onSelectTask(node.data);
}

export function TaskTree({ tasks, activeTaskId, onSelectTask, renderActions }: TaskTreeProps) {
  const model = useMemo(() => buildTaskTreeModel(tasks), [tasks]);
  const treeRef = useRef<TreeApi<TaskTreeNode> | undefined>(undefined);
  const syncFrameRef = useRef<number | null>(null);
  const [visibleNodeCount, setVisibleNodeCount] = useState(model.roots.length);

  const syncVisibleNodeCount = useCallback(() => {
    if (syncFrameRef.current !== null) cancelAnimationFrame(syncFrameRef.current);
    syncFrameRef.current = requestAnimationFrame(() => {
      syncFrameRef.current = null;
      setVisibleNodeCount(treeRef.current?.visibleNodes.length ?? model.roots.length);
    });
  }, [model.roots.length]);

  useLayoutEffect(() => {
    syncVisibleNodeCount();
  }, [model, syncVisibleNodeCount]);

  useEffect(() => () => {
    if (syncFrameRef.current !== null) cancelAnimationFrame(syncFrameRef.current);
  }, []);

  useEffect(() => {
    if (activeTaskId === null) return;
    treeRef.current?.openParents(String(activeTaskId));
  }, [activeTaskId, model]);

  if (model.roots.length === 0) return <p className="text-muted-foreground px-2 py-3 text-xs">暂无任务</p>;

  return (
    <div className="min-w-0 overflow-hidden rounded-lg">
      <Tree<TaskTreeNode>
        ref={treeRef}
        data={model.roots}
        idAccessor={(task) => String(task.task_id)}
        childrenAccessor={(task) => (task.children.length > 0 ? task.children : null)}
        width="100%"
        height={Math.min(MAX_TREE_HEIGHT, Math.max(ROW_HEIGHT, visibleNodeCount * ROW_HEIGHT))}
        className="[-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        rowHeight={ROW_HEIGHT}
        // The custom tree-line prefix already carries the complete visual
        // indentation, so Arborist's automatic padding must stay disabled.
        indent={0}
        overscanCount={5}
        openByDefault
        selection={activeTaskId === null ? undefined : String(activeTaskId)}
        disableMultiSelection
        disableDeselectOnClick
        disableDrag
        disableDrop
        disableEdit
        aria-label="Fork 任务树"
        onToggle={syncVisibleNodeCount}
        onActivate={(node) => activateTask(node, onSelectTask)}
      >
        {(props) => <TaskNode {...props} renderActions={renderActions} />}
      </Tree>
    </div>
  );
}
