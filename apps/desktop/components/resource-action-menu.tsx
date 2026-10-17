"use client";

import { MoreHorizontalIcon, Trash2Icon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

export function ResourceActionMenu({
  label,
  onDelete,
  tabIndex,
}: {
  label: string;
  onDelete: () => void;
  tabIndex?: number;
}) {
  return (
    <Popover>
      <PopoverTrigger
        type="button"
        aria-label={`更多 ${label} 操作`}
        tabIndex={tabIndex}
        className="focus-ring text-muted-foreground hover:bg-muted hover:text-foreground flex size-7 items-center justify-center rounded-md p-0"
      >
        <MoreHorizontalIcon className="size-4" />
      </PopoverTrigger>
      <PopoverContent align="end" className="w-40 p-1">
        <button
          type="button"
          className="text-destructive hover:bg-destructive/10 flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm"
          onClick={onDelete}
        >
          <Trash2Icon className="size-4" />
          删除
        </button>
      </PopoverContent>
    </Popover>
  );
}
