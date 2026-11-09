import { useEffect, useState, useSyncExternalStore } from "react";
import { PencilIcon, Trash2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import {
  readFavoritePrompts,
  subscribeFavoritePrompts,
  writeFavoritePrompts,
  type FavoritePrompt,
} from "@/lib/favorite-prompt-storage";

/**
 * 常用提示词库的管理对话框：新建、编辑、删除（删除先确认）。入口在输入框「+」菜单的提示词页。
 *
 * 副作用：读写本机提示词偏好存储；写入失败（存储空间不足等）在对话框内提示，不关闭对话框。
 * 每次打开都从「新建」状态开始。
 */
export function FavoritePromptManager({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const prompts = useSyncExternalStore(subscribeFavoritePrompts, readFavoritePrompts, readFavoritePrompts);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  const openNewPrompt = () => {
    setEditingId(null);
    setTitle("");
    setContent("");
    setError(null);
  };

  useEffect(() => {
    if (open) openNewPrompt();
  }, [open]);

  const editPrompt = (prompt: FavoritePrompt) => {
    setEditingId(prompt.id);
    setTitle(prompt.title);
    setContent(prompt.content);
    setError(null);
  };

  const persist = (nextPrompts: FavoritePrompt[]) => {
    if (!writeFavoritePrompts(nextPrompts)) {
      setError("保存失败，请检查本机存储空间后重试");
      return false;
    }
    setError(null);
    return true;
  };

  const savePrompt = () => {
    const nextTitle = title.trim();
    const nextContent = content.trim();
    if (!nextTitle || !nextContent) {
      setError("请填写提示词名称和内容");
      return;
    }
    const nextPrompts = editingId
      ? prompts.map((prompt) => prompt.id === editingId
        ? { ...prompt, title: nextTitle, content: nextContent }
        : prompt)
      : [...prompts, { id: crypto.randomUUID(), title: nextTitle, content: nextContent }];
    if (persist(nextPrompts)) openNewPrompt();
  };

  const deletePrompt = async (target: FavoritePrompt) => {
    const confirmed = await confirmDelete({
      target: `提示词「${target.title}」`,
      consequence: "删除后该提示词将从常用提示词列表中移除。",
    });
    if (!confirmed) return;
    // 以确认后的最新列表为准：确认框打开期间另一处输入框可能已修改提示词库。
    persist(readFavoritePrompts().filter((prompt) => prompt.id !== target.id));
    if (editingId === target.id) openNewPrompt();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>管理常用提示词</DialogTitle>
        </DialogHeader>
        <div className="grid min-h-0 gap-4 sm:grid-cols-[1fr_1.2fr]">
          <div className="flex min-h-48 flex-col gap-2">
            <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
              {prompts.map((prompt) => (
                <div key={prompt.id} className="hover:bg-muted flex items-center gap-1 rounded-lg px-2 py-1.5">
                  <button
                    type="button"
                    className="min-w-0 flex-1 truncate text-left text-sm focus-ring"
                    onClick={() => editPrompt(prompt)}
                    title={prompt.title}
                  >
                    {prompt.title}
                  </button>
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`编辑 ${prompt.title}`} onClick={() => editPrompt(prompt)}>
                    <PencilIcon />
                  </Button>
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`删除 ${prompt.title}`} onClick={() => void deletePrompt(prompt)}>
                    <Trash2Icon />
                  </Button>
                </div>
              ))}
              {prompts.length === 0 && <EmptyState size="compact" title="还没有保存的提示词" className="px-2 py-4" />}
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <label className="grid gap-1 text-xs font-medium">
              名称
              <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：代码审查" maxLength={80} />
            </label>
            <label className="grid gap-1 text-xs font-medium">
              提示词内容
              <Textarea
                value={content}
                onChange={(event) => setContent(event.target.value)}
                placeholder="输入要复用的提示词"
                rows={7}
                className="min-h-36 resize-y font-normal"
              />
            </label>
            {error && <Alert tone="destructive" className="text-xs">{error}</Alert>}
            <div className="flex justify-end gap-2 pt-1">
              <Button type="button" variant="ghost" size="sm" onClick={openNewPrompt}>新建</Button>
              <Button type="button" size="sm" onClick={savePrompt}>保存</Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
