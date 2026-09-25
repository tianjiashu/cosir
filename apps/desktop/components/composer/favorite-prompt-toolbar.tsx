"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { BookOpenTextIcon, MessageSquareQuoteIcon, PencilIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  readFavoritePrompts,
  subscribeFavoritePrompts,
  writeFavoritePrompts,
  type FavoritePrompt,
} from "@/lib/favorite-prompt-storage";
import { useInlineComposerInsertion } from "@/components/composer/inline-attachment-input";

type FavoritePromptToolbarProps = {
  disabled?: boolean;
};

/**
 * Rounded prompt picker and manager shared by the three composer surfaces.
 *
 * @param disabled - Prevents snippet insertion while keeping library management available.
 * @sideEffects Reads and writes the local prompt preference store; inserts text into the nearest composer.
 */
export function FavoritePromptToolbar({ disabled = false }: FavoritePromptToolbarProps) {
  const insertion = useInlineComposerInsertion();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const prompts = useSyncExternalStore(subscribeFavoritePrompts, readFavoritePrompts, readFavoritePrompts);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (managerOpen) {
      setEditingId(null);
      setTitle("");
      setContent("");
      setError(null);
    }
  }, [managerOpen]);

  const openNewPrompt = () => {
    setEditingId(null);
    setTitle("");
    setContent("");
    setError(null);
  };

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

  const deletePrompt = (promptId: string) => {
    persist(prompts.filter((prompt) => prompt.id !== promptId));
    if (editingId === promptId) openNewPrompt();
  };

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
        <PopoverTrigger
          type="button"
          className="border-border bg-background hover:bg-muted inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          aria-label="选择常用提示词"
        >
          <MessageSquareQuoteIcon className="size-3.5" />
          <span>常用提示词</span>
          <span className="text-muted-foreground tabular-nums">{prompts.length}</span>
        </PopoverTrigger>
        <PopoverContent align="start" side="top" className="w-80 rounded-xl p-2">
          <div className="flex items-center justify-between px-2 py-1">
            <p className="text-sm font-medium">选择提示词</p>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground rounded px-1.5 py-1 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                setPickerOpen(false);
                setManagerOpen(true);
              }}
            >
              管理
            </button>
          </div>
          <PromptList
            prompts={prompts}
            onSelect={(prompt) => {
              if (disabled) return;
              insertion.insertText(prompt.content);
              setPickerOpen(false);
            }}
            disabled={disabled}
          />
        </PopoverContent>
      </Popover>

      <Dialog open={managerOpen} onOpenChange={setManagerOpen}>
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
                      className="min-w-0 flex-1 truncate text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                      onClick={() => editPrompt(prompt)}
                      title={prompt.title}
                    >
                      {prompt.title}
                    </button>
                    <Button type="button" variant="ghost" size="icon-xs" aria-label={`编辑 ${prompt.title}`} onClick={() => editPrompt(prompt)}>
                      <PencilIcon />
                    </Button>
                    <Button type="button" variant="ghost" size="icon-xs" aria-label={`删除 ${prompt.title}`} onClick={() => deletePrompt(prompt.id)}>
                      <Trash2Icon />
                    </Button>
                  </div>
                ))}
                {prompts.length === 0 && <p className="text-muted-foreground px-2 py-4 text-xs">还没有保存的提示词</p>}
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <label className="grid gap-1 text-xs font-medium">
                名称
                <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：代码审查" maxLength={80} />
              </label>
              <label className="grid gap-1 text-xs font-medium">
                提示词内容
                <textarea
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  placeholder="输入要复用的提示词"
                  rows={7}
                  className="border-input bg-transparent placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 min-h-36 w-full resize-y rounded-lg border px-2.5 py-2 text-sm outline-none focus-visible:ring-2"
                />
              </label>
              {error && <p role="alert" className="text-destructive text-xs">{error}</p>}
              <div className="flex justify-end gap-2 pt-1">
                <Button type="button" variant="ghost" size="sm" onClick={openNewPrompt}>新建</Button>
                <Button type="button" size="sm" onClick={savePrompt}>保存</Button>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PromptList({
  prompts,
  onSelect,
  disabled,
}: {
  prompts: FavoritePrompt[];
  onSelect: (prompt: FavoritePrompt) => void;
  disabled: boolean;
}) {
  if (prompts.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 px-3 py-6 text-center">
        <BookOpenTextIcon className="text-muted-foreground size-5" />
        <p className="text-muted-foreground text-xs">还没有常用提示词，点击“管理”添加。</p>
      </div>
    );
  }

  return (
    <div role="group" aria-label="常用提示词" className="max-h-64 space-y-1 overflow-y-auto">
      {prompts.map((prompt) => (
        <button
          key={prompt.id}
          type="button"
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onSelect(prompt)}
          className="hover:bg-muted focus-visible:bg-muted flex w-full flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <span className="text-sm font-medium">{prompt.title}</span>
          <span className="text-muted-foreground line-clamp-2 text-xs">{prompt.content}</span>
        </button>
      ))}
    </div>
  );
}
