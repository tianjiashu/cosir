import { useState } from "react";
import { CheckIcon, ChevronsUpDownIcon, FolderIcon, FolderPlusIcon, Loader2Icon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { Workspace } from "@/lib/api/workspaces";

type WorkspacePickerProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  /** 从文件夹添加工作区（`useWorkspaceActions.addWorkspaceFromFolder`）：成功时已选中新工作区，返回 true。 */
  onAddWorkspace: () => Promise<boolean>;
  addingWorkspace: boolean;
};

/**
 * 新会话页标题里的工作区选择器，外观是命令行提示符的目录段（`otto ❯ 想做什么？` 里的 `otto`）。
 * 当前工作区只在这里与侧栏出现，不在输入框里另放一行；完整路径在悬停提示和下拉列表里。
 * 只发出选择意图；确认、提交选择和最近工作区偏好由布局目录边界统一处理。
 * 「新建工作区」与侧栏区头共用同一个添加动作，失败经全局提示呈现；成功后收起弹层。
 */
export function WorkspacePicker({ workspaces, selectedWorkspaceId, onWorkspaceChange, onAddWorkspace, addingWorkspace }: WorkspacePickerProps) {
  const [openMenu, setOpenMenu] = useState(false);
  const selected = workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId);

  const chooseWorkspace = (workspaceId: number) => {
    onWorkspaceChange(workspaceId);
    setOpenMenu(false);
  };

  const addFromFolder = async () => {
    if (await onAddWorkspace()) setOpenMenu(false);
  };

  return (
    <Popover open={openMenu} onOpenChange={setOpenMenu}>
      <PopoverTrigger
        type="button"
        aria-label={selected ? `选择工作区，当前 ${selected.name}` : "选择工作区"}
        title={selected?.root_path}
        className="hover:bg-muted focus-ring inline-flex min-w-0 max-w-64 items-center gap-1 rounded-md px-1.5 py-0.5 font-mono"
      >
        <span className="truncate">{selected?.name ?? "选择工作区"}</span>
        <ChevronsUpDownIcon className="text-muted-foreground size-4 shrink-0" />
      </PopoverTrigger>
      <PopoverContent align="start" side="bottom" className="w-80 p-2">
        <div className="mb-1 px-2 py-1"><p className="text-sm font-medium">选择工作区</p><p className="text-muted-foreground text-xs">对话和文件修改都会归属于所选目录</p></div>
        <div role="listbox" aria-label="工作区列表" className="max-h-64 overflow-y-auto">
          {workspaces.map((workspace) => {
            const active = workspace.workspace_id === selectedWorkspaceId;
            return <button key={workspace.workspace_id} type="button" role="option" aria-selected={active} onClick={() => chooseWorkspace(workspace.workspace_id)} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left focus-ring"><FolderIcon className="text-muted-foreground size-4 shrink-0" /><span className="min-w-0 flex-1"><span className="block truncate text-sm">{workspace.name}</span><span className="text-muted-foreground block truncate text-2xs">{workspace.root_path}</span></span>{active && <CheckIcon className="text-primary size-4 shrink-0" />}</button>;
          })}
        </div>
        <div className="border-border/60 mt-1 border-t pt-1"><button type="button" onClick={() => void addFromFolder()} disabled={addingWorkspace} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm focus-ring">{addingWorkspace ? <Loader2Icon className="size-4 animate-spin" /> : <FolderPlusIcon className="size-4" />}新建工作区</button></div>
      </PopoverContent>
    </Popover>
  );
}
