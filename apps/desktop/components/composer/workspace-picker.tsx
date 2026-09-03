"use client";

import { useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { CheckIcon, ChevronDownIcon, FolderIcon, FolderPlusIcon, Loader2Icon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { createWorkspace, type Workspace } from "@/lib/api/workspaces";
import { writeLastWorkspaceId } from "@/lib/workspace-preferences";

type WorkspacePickerProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: (workspace: Workspace) => Promise<void>;
  showCreateLabel?: boolean;
};

export function WorkspacePicker({ workspaces, selectedWorkspaceId, onWorkspaceChange, onWorkspaceCreated, showCreateLabel = false }: WorkspacePickerProps) {
  const [openMenu, setOpenMenu] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId);

  const chooseWorkspace = (workspaceId: number) => {
    writeLastWorkspaceId(workspaceId);
    onWorkspaceChange(workspaceId);
    setOpenMenu(false);
  };

  const createFromFolder = async () => {
    if (creating) return;
    setCreating(true);
    setError(null);
    try {
      const picked = await open({ directory: true, multiple: false, title: "选择工作区文件夹" });
      if (typeof picked !== "string") return;
      const name = picked.split(/[\\/]/).filter(Boolean).at(-1) ?? "新工作区";
      const workspace = await createWorkspace({ name, root_path: picked });
      await onWorkspaceCreated(workspace);
      chooseWorkspace(workspace.workspace_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建工作区失败，请重试");
    } finally {
      setCreating(false);
    }
  };

  return (
    <Popover open={openMenu} onOpenChange={setOpenMenu}>
      <PopoverTrigger type="button" className={`border-border bg-background hover:bg-muted inline-flex min-w-0 max-w-64 items-center justify-start gap-1.5 rounded-lg border font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50 ${showCreateLabel ? "h-7 px-2.5 text-xs" : "h-8 px-2.5 text-sm"}`}>
          <FolderIcon className="size-4 shrink-0" />
          <span className="truncate">{selected?.name ?? "选择工作区"}</span>
          <ChevronDownIcon className="text-muted-foreground ml-auto size-3.5 shrink-0" />
      </PopoverTrigger>
      <PopoverContent align="start" side="top" className="w-80 p-2">
        <div className="mb-1 px-2 py-1"><p className="text-sm font-medium">选择工作区</p><p className="text-muted-foreground text-xs">对话和文件修改都会归属于所选目录</p></div>
        <div role="listbox" aria-label="工作区列表" className="max-h-64 overflow-y-auto">
          {workspaces.map((workspace) => {
            const active = workspace.workspace_id === selectedWorkspaceId;
            return <button key={workspace.workspace_id} type="button" role="option" aria-selected={active} onClick={() => chooseWorkspace(workspace.workspace_id)} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50"><FolderIcon className="text-muted-foreground size-4 shrink-0" /><span className="min-w-0 flex-1"><span className="block truncate text-sm">{workspace.name}</span><span className="text-muted-foreground block truncate text-[11px]">{workspace.root_path}</span></span>{active && <CheckIcon className="text-primary size-4 shrink-0" />}</button>;
          })}
        </div>
        <div className="border-border/60 mt-1 border-t pt-1"><button type="button" onClick={() => void createFromFolder()} disabled={creating} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50">{creating ? <Loader2Icon className="size-4 animate-spin" /> : <FolderPlusIcon className="size-4" />}新建工作区</button></div>
        {error && <p className="text-destructive px-2 pt-2 text-xs">{error}</p>}
      </PopoverContent>
    </Popover>
  );
}
