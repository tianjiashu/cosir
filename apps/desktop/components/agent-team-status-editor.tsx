"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { PlusIcon, XIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import type { AgentTeamStatusConfiguration } from "@/lib/api/agent-teams";
import { DEFAULT_AGENT_TEAM_STATUS_COLOR } from "@/lib/agent-team-status";
import { isImeComposing } from "@/lib/keyboard/ime";

export function splitStatusValues(value: string): string[] {
  return value.split(/[,，\r\n]/);
}

export function AgentTeamStatusEditor({
  statuses,
  onAdd,
  onRemove,
  onColorChange,
}: {
  statuses: AgentTeamStatusConfiguration[];
  onAdd: (statuses: AgentTeamStatusConfiguration[]) => void;
  onRemove: (index: number) => void;
  onColorChange: (index: number, color: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const [newStatusColor, setNewStatusColor] = useState(DEFAULT_AGENT_TEAM_STATUS_COLOR);
  const inputRef = useRef<HTMLInputElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isAdding) inputRef.current?.focus();
  }, [isAdding]);

  const add = (values: string[]) => {
    const additions = values
      .map((name) => name.trim())
      .filter(Boolean)
      .map((name) => ({ name, color: newStatusColor }));
    if (additions.length > 0) onAdd(additions);
  };

  const handleInputChange = (value: string) => {
    const parts = value.split(/[,，\r\n]/);
    const hasTrailingSeparator = /[,，\r\n]$/.test(value);
    const nextDraft = hasTrailingSeparator ? "" : parts.pop() ?? "";
    if (hasTrailingSeparator) parts.pop();
    add(parts);
    setDraft(nextDraft);
  };

  const commitDraft = () => {
    add([draft]);
    setDraft("");
    setIsAdding(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (isImeComposing(event)) return;
    if (event.key === "Enter") {
      event.preventDefault();
      commitDraft();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setDraft("");
      setIsAdding(false);
      return;
    }
    if (event.key === "Backspace" && !draft && statuses.length > 0) {
      event.preventDefault();
      onRemove(statuses.length - 1);
    }
  };

  return (
    <div ref={editorRef} className="space-y-1" onBlur={(event) => {
      if (editorRef.current?.contains(event.relatedTarget as Node | null)) return;
      if (isAdding) commitDraft();
    }}>
      <div className="flex min-h-8 flex-wrap items-center gap-1.5">
        {statuses.map((status, index) => (
          <span key={`${status.name}:${index}`} className="border-border bg-background text-foreground inline-flex h-7 items-center gap-1.5 rounded-full border px-2 text-xs">
            <label className="relative inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-full focus-within:ring-3 focus-within:ring-ring/50" title={`选择 ${status.name} 的颜色`}>
              <span className="border-foreground/20 size-3 rounded-full border" style={{ backgroundColor: status.color }} />
              <input
                type="color"
                aria-label={`状态 ${status.name} 的颜色`}
                value={status.color}
                onChange={(event) => onColorChange(index, event.target.value.toUpperCase())}
                className="absolute inset-0 size-full cursor-pointer opacity-0"
              />
            </label>
            <span>{status.name}</span>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground inline-flex size-4 items-center justify-center rounded-full transition-colors focus-ring"
              aria-label={`移除状态 ${status.name}`}
              onClick={() => onRemove(index)}
            >
              <XIcon className="size-3" />
            </button>
          </span>
        ))}
        {isAdding ? (
          <span className="border-input bg-background inline-flex h-8 items-center gap-2 rounded-full border px-2">
            <label className="relative inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-full focus-within:ring-3 focus-within:ring-ring/50" title="选择新状态的颜色">
              <span className="border-foreground/20 size-4 rounded-full border" style={{ backgroundColor: newStatusColor }} />
              <input
                type="color"
                aria-label="新状态颜色"
                value={newStatusColor}
                onChange={(event) => setNewStatusColor(event.target.value.toUpperCase())}
                className="absolute inset-0 size-full cursor-pointer opacity-0"
              />
            </label>
            <Input
              ref={inputRef}
              value={draft}
              onChange={(event) => handleInputChange(event.target.value)}
              onPaste={(event) => {
                const pasted = event.clipboardData.getData("text");
                if (!/[,，\r\n]/.test(pasted)) return;
                event.preventDefault();
                add(splitStatusValues(draft ? `${draft},${pasted}` : pasted));
                setDraft("");
              }}
              onKeyDown={handleKeyDown}
              className="h-7 w-28 border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-0 dark:bg-transparent"
              placeholder="状态名 · Enter 保存"
              aria-label="添加业务状态"
            />
          </span>
        ) : (
          <button
            type="button"
            className="border-border bg-background text-muted-foreground hover:text-foreground inline-flex h-7 items-center gap-1 rounded-full border border-dashed px-2.5 text-xs transition-colors hover:bg-muted focus-ring"
            onClick={() => setIsAdding(true)}
          >
            <PlusIcon className="size-3.5" />
            添加状态
          </button>
        )}
      </div>
    </div>
  );
}
