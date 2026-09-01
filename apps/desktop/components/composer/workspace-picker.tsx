"use client";

import { useRef, useState } from "react";
import { CheckIcon, ChevronDownIcon, FolderIcon, FolderPlusIcon, Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createWorkspace, type Workspace } from "@/lib/api/workspaces";

type WorkspacePickerProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: (workspace: Workspace) => Promise<void>;
  showCreateLabel?: boolean;
};

export function WorkspacePicker({
  workspaces,
  selectedWorkspaceId,
  onWorkspaceChange,
  onWorkspaceCreated,
  showCreateLabel = false,
}: WorkspacePickerProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("");
  const [rootPath, setRootPath] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const handleCreate = async () => {
    const trimmedName = name.trim();
    const trimmedPath = rootPath.trim();
    if (!trimmedName || !trimmedPath || creating) return;
    setCreating(true);
    setError(null);
    try {
      const workspace = await createWorkspace({ name: trimmedName, root_path: trimmedPath });
      await onWorkspaceCreated(workspace);
      onWorkspaceChange(workspace.workspace_id);
      setName("");
      setRootPath("");
      dialogRef.current?.close();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建工作区失败，请重试");
    } finally {
      setCreating(false);
    }
  };

  return (
    <>
      <div className="border-border/60 bg-background/80 text-foreground flex h-8 min-w-0 items-center gap-1.5 rounded-lg border px-2 shadow-xs">
        <FolderIcon className="text-muted-foreground size-3.5 shrink-0" />
        {workspaces.length > 0 ? (
          <span className="flex min-w-0 items-center">
            <select
              aria-label="选择工作区"
              value={selectedWorkspaceId ?? ""}
              onChange={(event) => onWorkspaceChange(Number(event.target.value))}
              className="max-w-48 cursor-pointer appearance-none bg-transparent py-1.5 pr-1 text-xs font-medium outline-none"
            >
              {!selectedWorkspaceId && <option value="">选择工作区</option>}
              {workspaces.map((workspace) => (
                <option key={workspace.workspace_id} value={workspace.workspace_id}>
                  {workspace.name}
                </option>
              ))}
            </select>
            <ChevronDownIcon className="text-muted-foreground pointer-events-none -ml-1 size-3" />
          </span>
        ) : (
          <span className="text-muted-foreground py-1.5 text-xs">选择工作区</span>
        )}
        <Button
          type="button"
          variant="ghost"
          size={showCreateLabel ? "sm" : "icon-xs"}
          className="text-muted-foreground hover:text-foreground"
          onClick={() => { setError(null); dialogRef.current?.showModal(); }}
          aria-label="创建工作区"
          title="创建工作区"
        >
          <FolderPlusIcon />
          {showCreateLabel && "创建工作区"}
        </Button>
      </div>
      <dialog
        ref={dialogRef}
        aria-labelledby="create-workspace-title"
        className="bg-popover text-popover-foreground backdrop:bg-black/20 m-auto w-[min(28rem,calc(100%-2rem))] rounded-xl border p-0 shadow-xl"
        onCancel={(event) => { if (creating) event.preventDefault(); }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
            event.preventDefault();
            void handleCreate();
          }
        }}
      >
        <div className="space-y-4 p-5">
          <div>
            <h2 id="create-workspace-title" className="font-medium">创建工作区</h2>
            <p className="text-muted-foreground mt-1 text-sm">新任务和后续修改都会归属于这个目录。</p>
          </div>
          <label className="block space-y-1.5 text-sm">
            <span>名称</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：我的项目" autoFocus className="border-input bg-background w-full rounded-md border px-3 py-2 outline-none focus:ring-2 focus:ring-ring/20" />
          </label>
          <label className="block space-y-1.5 text-sm">
            <span>本地目录</span>
            <input value={rootPath} onChange={(event) => setRootPath(event.target.value)} placeholder="例如：H:\\coding-agent" className="border-input bg-background w-full rounded-md border px-3 py-2 outline-none focus:ring-2 focus:ring-ring/20" />
          </label>
          {error && <p className="text-destructive text-sm">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => { if (!creating) dialogRef.current?.close(); }}>取消</Button>
            <Button type="button" onClick={() => void handleCreate()} disabled={!name.trim() || !rootPath.trim() || creating}>
              {creating ? <Loader2Icon className="animate-spin" /> : <CheckIcon />}创建工作区
            </Button>
          </div>
        </div>
      </dialog>
    </>
  );
}
