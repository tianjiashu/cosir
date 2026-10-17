"use client";

import { Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";

export function ResourceDeleteButton({
  label,
  onDelete,
  tabIndex,
}: {
  label: string;
  onDelete: () => void;
  tabIndex?: number;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      tabIndex={tabIndex}
      className="text-destructive hover:bg-destructive/10 hover:text-destructive"
      aria-label={`删除 ${label}`}
      title={`删除 ${label}`}
      onClick={onDelete}
    >
      <Trash2Icon className="size-4" />
    </Button>
  );
}
