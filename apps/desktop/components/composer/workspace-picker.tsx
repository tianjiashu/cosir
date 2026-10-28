"use client";

import { useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { CheckIcon, ChevronDownIcon, FolderIcon, FolderPlusIcon, Loader2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { createWorkspace, type Workspace } from "@/lib/api/workspaces";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";

type WorkspacePickerProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: (workspace: Workspace) => Promise<void>;
};

/**
 * 「从文件夹添加工作区」流程：系统目录选择 → 创建 → 等宿主刷新目录 → 选中新工作区。
 *
 * 工作区选择器与首次使用引导共用这一个入口，避免两处各写一份创建与选中逻辑。
 * 用户取消目录选择时返回 false 且不报错；失败写入 error 供调用方就地展示。
 */
export function useCreateWorkspaceFromFolder({ onWorkspaceCreated, onWorkspaceChange }: {
  onWorkspaceCreated: (workspace: Workspace) => Promise<void>;
  onWorkspaceChange: (workspaceId: number) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async (): Promise<boolean> => {
    if (creating) return false;
    setCreating(true);
    setError(null);
    try {
      const picked = await open({ directory: true, multiple: false, title: "选择工作区文件夹" });
      if (typeof picked !== "string") return false;
      const name = picked.split(/[\\/]/).filter(Boolean).at(-1) ?? "新工作区";
      const workspace = await createWorkspace({ name, root_path: picked });
      await onWorkspaceCreated(workspace);
      onWorkspaceChange(workspace.workspace_id);
      return true;
    } catch (cause) {
      setError(userErrorMessage(cause, "创建工作区失败，请重试"));
      void frontendLog("ERROR", "workspace_create_from_folder_failed", "从文件夹创建工作区失败", { error: cause });
      return false;
    } finally {
      setCreating(false);
    }
  };

  return { create, creating, error };
}

/** 只发出选择意图；确认、提交选择和最近工作区偏好由布局目录边界统一处理。 */
export function WorkspacePicker({ workspaces, selectedWorkspaceId, onWorkspaceChange, onWorkspaceCreated }: WorkspacePickerProps) {
  const [openMenu, setOpenMenu] = useState(false);
  const { create, creating, error } = useCreateWorkspaceFromFolder({ onWorkspaceCreated, onWorkspaceChange });
  const selected = workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId);

  const chooseWorkspace = (workspaceId: number) => {
    onWorkspaceChange(workspaceId);
    setOpenMenu(false);
  };

  const createFromFolder = async () => {
    if (await create()) setOpenMenu(false);
  };

  return (
    <Popover open={openMenu} onOpenChange={setOpenMenu}>
      <PopoverTrigger type="button" className="border-border bg-background hover:bg-muted inline-flex min-w-0 max-w-64 items-center justify-start gap-1.5 rounded-lg border font-medium focus-ring h-8 px-2.5 text-sm">
          <FolderIcon className="size-4 shrink-0" />
          <span className="truncate">{selected?.name ?? "选择工作区"}</span>
          <ChevronDownIcon className="text-muted-foreground ml-auto size-3.5 shrink-0" />
      </PopoverTrigger>
      <PopoverContent align="start" side="top" className="w-80 p-2">
        <div className="mb-1 px-2 py-1"><p className="text-sm font-medium">选择工作区</p><p className="text-muted-foreground text-xs">对话和文件修改都会归属于所选目录</p></div>
        <div role="listbox" aria-label="工作区列表" className="max-h-64 overflow-y-auto">
          {workspaces.map((workspace) => {
            const active = workspace.workspace_id === selectedWorkspaceId;
            return <button key={workspace.workspace_id} type="button" role="option" aria-selected={active} onClick={() => chooseWorkspace(workspace.workspace_id)} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left focus-ring"><FolderIcon className="text-muted-foreground size-4 shrink-0" /><span className="min-w-0 flex-1"><span className="block truncate text-sm">{workspace.name}</span><span className="text-muted-foreground block truncate text-2xs">{workspace.root_path}</span></span>{active && <CheckIcon className="text-primary size-4 shrink-0" />}</button>;
          })}
        </div>
        <div className="border-border/60 mt-1 border-t pt-1"><button type="button" onClick={() => void createFromFolder()} disabled={creating} className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm focus-ring">{creating ? <Loader2Icon className="size-4 animate-spin" /> : <FolderPlusIcon className="size-4" />}新建工作区</button></div>
        {error && <Alert tone="destructive" className="mt-2 text-xs">{error}</Alert>}
      </PopoverContent>
    </Popover>
  );
}
