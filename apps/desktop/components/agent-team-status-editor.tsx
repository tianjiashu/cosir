"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { PlusIcon, XIcon } from "lucide-react";

import { Input } from "@/components/ui/input";

export function splitStatusValues(value: string): string[] {
  return value.split(/[,，\r\n]/);
}

export function AgentTeamStatusEditor({
  statuses,
  onAdd,
  onRemove,
}: {
  statuses: string[];
  onAdd: (statuses: string[]) => void;
  onRemove: (index: number) => void;
}) {
  const [draft, setDraft] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isAdding) inputRef.current?.focus();
  }, [isAdding]);

  const add = (values: string[]) => {
    onAdd(values);
  };

  const handleInputChange = (value: string) => {
    const parts = value.split(/[,，\r\n]/);
    const hasTrailingSeparator = /[,，\r\n]$/.test(value);
    const nextDraft = hasTrailingSeparator ? "" : parts.pop() ?? "";
    if (hasTrailingSeparator) parts.pop();
    add(parts);
    setDraft(nextDraft);
  };

  const commitDraft = (allowEmpty = false) => {
    if (draft !== "" || allowEmpty) add([draft]);
    setDraft("");
    setIsAdding(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      commitDraft(true);
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
    <div className="space-y-1">
      <div className="flex min-h-8 flex-wrap items-center gap-1.5">
        {statuses.map((status, index) => (
          <span key={`${status}:${index}`} className="border-border bg-background text-foreground inline-flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs">
            <span>{status}</span>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground inline-flex size-4 items-center justify-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`移除状态 ${status}`}
              onClick={() => onRemove(index)}
            >
              <XIcon className="size-3" />
            </button>
          </span>
        ))}
        {isAdding ? (
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
            onBlur={() => commitDraft()}
            className="h-7 w-40 rounded-full border-border bg-background px-3 text-xs shadow-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20"
            placeholder="Enter 保存"
            aria-label="添加业务状态"
          />
        ) : (
          <button
            type="button"
            className="border-border bg-background text-muted-foreground hover:text-foreground inline-flex h-7 items-center gap-1 rounded-full border border-dashed px-2.5 text-xs transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
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
