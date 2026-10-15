import { type FC } from "react";
import { CheckCheckIcon, ListXIcon, WrenchIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EmptyState, LoadingState } from "@/components/ui/state-panel";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type ToolGroupSelectorProps = {
  toolGroups: ToolGroupCatalog[];
  selectedToolGroups: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
};

/** 提供本轮对话使用的工具组禁用选择，并支持批量切换。 */
export const ToolGroupSelector: FC<ToolGroupSelectorProps> = ({
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  loading = false,
  error,
  disabled = false,
}) => {
  const selected = new Set(selectedToolGroups);
  const selectedCount = toolGroups.filter(({ group }) => selected.has(group)).length;
  const canEdit = Boolean(onSelectedToolGroupsChange)
    && !disabled
    && !loading
    && !error
    && toolGroups.length > 0;
  const allSelected = toolGroups.length > 0
    && selectedCount === toolGroups.length
    && selectedToolGroups.length === toolGroups.length;
  const noneSelected = selectedToolGroups.length === 0;

  return (
    <Popover>
      <PopoverTrigger
        type="button"
        disabled={disabled}
        className="border-border bg-background hover:bg-muted inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium focus-ring disabled:cursor-not-allowed disabled:opacity-50"
        aria-label="选择禁用工具组"
      >
        <WrenchIcon className="size-3.5" />
        <span>禁用工具组</span>
        <Badge size="sm" className="tabular-nums">
          （{selectedCount}）
        </Badge>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-72 rounded-xl p-2 shadow-lg">
        <div className="border-border/70 flex items-center justify-between gap-2 border-b px-1 pb-2">
          <div className="flex min-w-0 items-baseline gap-1.5">
            <p className="truncate text-sm font-medium">禁用工具组</p>
            <span className="text-muted-foreground shrink-0 text-2xs tabular-nums">
              {selectedCount}/{toolGroups.length}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canEdit || allSelected}
              onClick={() => onSelectedToolGroupsChange?.(toolGroups.map(({ group }) => group))}
              className="text-muted-foreground px-2 font-normal"
            >
              <CheckCheckIcon className="size-3" />
              全选
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canEdit || noneSelected}
              onClick={() => onSelectedToolGroupsChange?.([])}
              className="text-muted-foreground px-2 font-normal"
            >
              <ListXIcon className="size-3" />
              全不选
            </Button>
          </div>
        </div>
        {loading && <LoadingState size="compact" label="正在加载工具组…" className="px-1" />}
        {error && <Alert tone="destructive" className="mt-2">工具组加载失败：{error}</Alert>}
        {!loading && !error && toolGroups.length === 0 && (
          <EmptyState size="compact" title="当前没有可配置的工具组" className="px-1" />
        )}
        {!loading && !error && toolGroups.length > 0 && (
          <div role="group" aria-label="禁用工具组列表" className="max-h-64 space-y-0.5 overflow-y-auto pt-1.5">
            {toolGroups.map(({ group, tools }) => {
              const isSelected = selected.has(group);
              return (
                <label
                  key={group}
                  className={`group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
                    isSelected
                      ? "bg-muted/70"
                      : "hover:bg-muted/60"
                  } ${disabled ? "cursor-not-allowed opacity-55" : ""}`}
                >
                  <Checkbox
                    checked={isSelected}
                    disabled={disabled || !onSelectedToolGroupsChange}
                    onChange={(event) => {
                      const next = new Set(selectedToolGroups);
                      if (event.currentTarget.checked) next.add(group);
                      else next.delete(group);
                      onSelectedToolGroupsChange?.([...next]);
                    }}
                  />
                  <span className="min-w-0 flex-1 truncate">{group}</span>
                  <span className="text-muted-foreground min-w-5 text-right text-xs tabular-nums">
                    {tools.length}
                  </span>
                </label>
              );
            })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
};
