import { type FC } from "react";
import { CheckCheckIcon, ListXIcon, WrenchIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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
        className="border-border bg-background hover:bg-muted inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
        aria-label="选择禁用工具组"
      >
        <WrenchIcon className="size-3.5" />
        <span>禁用工具组</span>
        <span className="bg-muted text-muted-foreground rounded-full px-1.5 py-0.5 text-[10px] leading-none tabular-nums">
          （{selectedCount}）
        </span>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-72 rounded-xl p-2 shadow-lg">
        <div className="border-border/70 flex items-center justify-between gap-2 border-b px-1 pb-2">
          <div className="flex min-w-0 items-baseline gap-1.5">
            <p className="truncate text-sm font-medium">禁用工具组</p>
            <span className="text-muted-foreground shrink-0 text-[11px] tabular-nums">
              {selectedCount}/{toolGroups.length}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              disabled={!canEdit || allSelected}
              onClick={() => onSelectedToolGroupsChange?.(toolGroups.map(({ group }) => group))}
              className="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:opacity-40"
            >
              <CheckCheckIcon className="size-3" />
              全选
            </button>
            <button
              type="button"
              disabled={!canEdit || noneSelected}
              onClick={() => onSelectedToolGroupsChange?.([])}
              className="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:opacity-40"
            >
              <ListXIcon className="size-3" />
              全不选
            </button>
          </div>
        </div>
        {loading && <p className="text-muted-foreground px-1 py-3 text-sm">正在加载工具组…</p>}
        {error && <p className="text-destructive px-1 py-3 text-sm">工具组加载失败：{error}</p>}
        {!loading && !error && toolGroups.length === 0 && (
          <p className="text-muted-foreground px-1 py-3 text-sm">当前没有可配置的工具组</p>
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
                  <input
                    type="checkbox"
                    checked={isSelected}
                    disabled={disabled || !onSelectedToolGroupsChange}
                    className="accent-primary size-4 shrink-0 rounded border-input focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
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
