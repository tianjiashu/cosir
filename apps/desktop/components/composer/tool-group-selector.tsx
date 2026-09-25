import { type FC } from "react";
import { WrenchIcon } from "lucide-react";

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

/** Render the shared run-scoped tool group selection control. */
export const ToolGroupSelector: FC<ToolGroupSelectorProps> = ({
  toolGroups,
  selectedToolGroups,
  onSelectedToolGroupsChange,
  loading = false,
  error,
  disabled = false,
}) => {
  const selected = new Set(selectedToolGroups);

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
        <span className="text-muted-foreground tabular-nums">{selected.size}</span>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" className="w-72 rounded-xl p-2">
        {loading && <p className="text-muted-foreground px-2 py-1">正在加载工具组…</p>}
        {error && <p className="text-destructive px-2 py-1">工具组加载失败：{error}</p>}
        {!loading && !error && toolGroups.length === 0 && (
          <p className="text-muted-foreground px-2 py-1">当前没有可配置的工具组</p>
        )}
        <div role="group" aria-label="禁用工具组列表" className="max-h-64 space-y-0.5 overflow-y-auto">
          {toolGroups.map(({ group, tools }) => (
            <label key={group} className="hover:bg-muted flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm">
              <input
                type="checkbox"
                checked={selected.has(group)}
                disabled={disabled || Boolean(error)}
                className="accent-primary size-4 shrink-0 rounded border-input focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                onChange={(event) => {
                  const next = new Set(selectedToolGroups);
                  if (event.currentTarget.checked) next.add(group);
                  else next.delete(group);
                  onSelectedToolGroupsChange?.([...next]);
                }}
              />
              <span className="flex-1">{group}</span>
              <span className="text-muted-foreground tabular-nums text-xs">{tools.length}</span>
            </label>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
};
