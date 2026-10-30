import { useMemo, useState } from "react";
import { MessageSquareIcon } from "lucide-react";

import { TaskRunStatusDot } from "@/components/task-tree/task-run-status";
import type { WorkspaceTreeSource } from "@/components/task-tree/workspace-tree-model";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import type { WorkspaceTask } from "@/lib/api/workspaces";
import { formatRelativeTime } from "@/lib/format/relative-time";
import { shortcutLabel } from "@/lib/shortcuts";

import { buildCommandPaletteModel, type CommandPaletteCommand } from "./command-palette-model";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";

export type CommandPaletteProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaces: WorkspaceTreeSource[];
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  onNewConversation: () => void;
  onNavigate: (path: string) => void;
};

/**
 * ⌘K 命令面板：跨工作区搜索任务、执行固定操作、跳转配置页。
 *
 * 只消费 props，不自行取数或导航——数据与跳转由 AppLayout 提供，面板因此不依赖路由与查询缓存。
 * 过滤 / 排序 / 截断在 command-palette-model 中完成，cmdk 以 `shouldFilter={false}` 只负责
 * 键盘导航与选中态。IME 安全由 cmdk 自身保证：其根节点 keydown 在 `isComposing` 或
 * keyCode 229 时不处理 Enter / 方向键，组字确认候选词不会误选条目。
 *
 * 选中任一条目先关闭面板再回调，查询词在关闭时清空，下次打开从最近任务开始。
 */
export function CommandPalette({ open, onOpenChange, workspaces, onSelectTask, onNewConversation, onNavigate }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const model = useMemo(() => buildCommandPaletteModel(workspaces, query), [workspaces, query]);
  const now = new Date();

  const changeOpen = (next: boolean) => {
    if (!next) setQuery("");
    onOpenChange(next);
  };

  const runCommand = (command: CommandPaletteCommand) => {
    changeOpen(false);
    if (command.kind === "new-conversation") onNewConversation();
    else onNavigate(command.path);
  };

  return (
    <CommandDialog open={open} onOpenChange={changeOpen} title="命令面板" description="搜索任务、操作与配置">
      <Command shouldFilter={false}>
        <CommandInput value={query} onValueChange={setQuery} placeholder="搜索任务、操作与配置…" aria-label="搜索任务、操作与配置" />
        <CommandList>
          <CommandEmpty>没有匹配的结果</CommandEmpty>
          {model.tasks.length > 0 ? (
            <CommandGroup heading={query.trim() ? "任务" : "最近任务"}>
              {model.tasks.map(({ task, workspaceId, workspaceName }) => (
                <CommandItem
                  key={task.task_id}
                  value={`task:${task.task_id}`}
                  onSelect={() => {
                    changeOpen(false);
                    onSelectTask(task, workspaceId);
                  }}
                >
                  <MessageSquareIcon className="text-muted-foreground" />
                  <span className="min-w-0 truncate">{task.title}</span>
                  <TaskStatusDot taskId={task.task_id} />
                  <span className="shrink-0 truncate text-xs text-muted-foreground">{workspaceName}</span>
                  <CommandShortcut className="shrink-0 tracking-normal">{formatRelativeTime(task.updated_at, now)}</CommandShortcut>
                </CommandItem>
              ))}
            </CommandGroup>
          ) : null}
          {model.actions.length > 0 ? (
            <CommandGroup heading="操作">
              {model.actions.map((command) => <CommandRow key={command.id} command={command} onRun={runCommand} />)}
            </CommandGroup>
          ) : null}
          {model.settings.length > 0 ? (
            <CommandGroup heading="配置">
              {model.settings.map((command) => <CommandRow key={command.id} command={command} onRun={runCommand} />)}
            </CommandGroup>
          ) : null}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

function CommandRow({ command, onRun }: { command: CommandPaletteCommand; onRun: (command: CommandPaletteCommand) => void }) {
  const Icon = command.icon;
  return (
    <CommandItem value={command.id} onSelect={() => onRun(command)}>
      <Icon className="text-muted-foreground" />
      <span>{command.label}</span>
      {command.shortcut ? <CommandShortcut>{shortcutLabel(command.shortcut)}</CommandShortcut> : null}
    </CommandItem>
  );
}

function TaskStatusDot({ taskId }: { taskId: number }) {
  return <TaskRunStatusDot status={useTaskExecutionStatus(taskId)} />;
}
