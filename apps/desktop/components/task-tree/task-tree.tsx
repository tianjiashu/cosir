import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, GitForkIcon, MessageSquareIcon } from "lucide-react";
import { getTreeLinePrefix, Tree, type NodeRendererProps, type NodeApi, type TreeApi } from "react-arborist";

import type { WorkspaceTask } from "@/lib/api/workspaces";
import { cn } from "cn";
import { buildTaskTreeModel, type TaskTreeNode } from "./task-tree-model";

type TaskTreeProps = {
  tasks: WorkspaceTask[];
  activeTaskId: number | null;
  onSelectTask: (task: WorkspaceTask) => void;
  renderActions: (task: WorkspaceTask) => ReactNode;
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
        "group flex min-w-0 items-center rounded-lg pr-1 text-xs transition-colors",
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
        aria-label={hasChildren ? (node.isOpen ? `收起${task.title}` : `展开${task.title}`) : undefined}
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
      <div className="flex min-w-0 flex-1 items-center gap-2 py-1.5" title={task.title}>
            {isFork ? <GitForkIcon className="text-sky-600 dark:text-sky-400 size-3.5 shrink-0" aria-hidden="true" /> : <MessageSquareIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />}
            <span className="min-w-0 flex-1 truncate font-medium">{task.title}</span>
      </div>
      <div className="ml-1 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100" onClick={(event) => event.stopPropagation()}>
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
        height={Math.min(MAX_TREE_HEIGHT, Math.max(ROW_HEIGHT, tasks.length * ROW_HEIGHT))}
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
        onActivate={(node) => activateTask(node, onSelectTask)}
      >
        {(props) => <TaskNode {...props} renderActions={renderActions} />}
      </Tree>
    </div>
  );
}
