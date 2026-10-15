import { useId, type FC } from "react";
import { WrenchIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EmptyState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type ToolGroupSelectorProps = {
  toolGroups: ToolGroupCatalog[];
  /**
   * 被关闭（禁用）的工具组。名称沿用上游 ComposerPromptToolbar 的 props；持久化与 ban-tools
   * 命令都以禁用列表为契约，界面只是反过来呈现成开关。
   */
  selectedToolGroups: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
};

/**
 * 本轮对话可用工具组的开关面板。
 *
 * 用户心智是「开着哪些工具」，因此开关为开表示可用；数据仍是禁用列表（上游存储与
 * 后端 ban-tools 契约不变），在这里做一次取反。触发按钮只在有组被关闭时提示数量，
 * 默认全开时保持安静。
 */
export const ToolGroupSelector: FC<ToolGroupSelectorProps> = ({
  toolGroups,
  selectedToolGroups: disabledToolGroups,
  onSelectedToolGroupsChange: onDisabledToolGroupsChange,
  loading = false,
  error,
  disabled = false,
}) => {
  const idPrefix = useId();
  const disabledSet = new Set(disabledToolGroups);
  const disabledCount = toolGroups.filter(({ group }) => disabledSet.has(group)).length;
  const enabledCount = toolGroups.length - disabledCount;
  const canEdit = Boolean(onDisabledToolGroupsChange)
    && !disabled
    && !loading
    && !error
    && toolGroups.length > 0;

  const setGroupEnabled = (group: string, enabled: boolean) => {
    const next = new Set(disabledToolGroups);
    if (enabled) next.delete(group);
    else next.add(group);
    onDisabledToolGroupsChange?.([...next]);
  };

  return (
    <Popover>
      <PopoverTrigger
        type="button"
        disabled={disabled}
        className="border-border bg-background hover:bg-muted inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium focus-ring disabled:cursor-not-allowed disabled:opacity-50"
        aria-label={disabledCount > 0 ? `工具，已关闭 ${disabledCount} 组` : "工具"}
      >
        <WrenchIcon className="size-3.5" />
        <span>工具</span>
        {disabledCount > 0 && (
          <Badge size="sm" tone="warning" className="tabular-nums">已关闭 {disabledCount}</Badge>
        )}
      </PopoverTrigger>
      <PopoverContent align="start" side="top" sideOffset={6} className="w-80 rounded-xl p-2 shadow-lg">
        <div className="border-border/70 flex items-center justify-between gap-2 border-b px-1 pb-2">
          <div className="flex min-w-0 items-baseline gap-1.5">
            <p className="truncate text-sm font-medium">工具</p>
            <span className="text-muted-foreground shrink-0 text-2xs tabular-nums">
              已开启 {enabledCount}/{toolGroups.length}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canEdit || disabledCount === 0}
              onClick={() => onDisabledToolGroupsChange?.([])}
              className="text-muted-foreground px-2 font-normal"
            >
              全部开启
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canEdit || enabledCount === 0}
              onClick={() => onDisabledToolGroupsChange?.(toolGroups.map(({ group }) => group))}
              className="text-muted-foreground px-2 font-normal"
            >
              全部关闭
            </Button>
          </div>
        </div>
        {loading && <LoadingState size="compact" label="正在加载工具…" className="px-1" />}
        {error && <Alert tone="destructive" className="mt-2">工具加载失败：{error}</Alert>}
        {!loading && !error && toolGroups.length === 0 && (
          <EmptyState size="compact" title="当前没有可配置的工具" className="px-1" />
        )}
        {!loading && !error && toolGroups.length > 0 && (
          <div role="group" aria-label="工具组" className="max-h-72 space-y-0.5 overflow-y-auto pt-1.5">
            {toolGroups.map(({ group, tools }, index) => {
              const enabled = !disabledSet.has(group);
              // 组名可能含空格，不能直接拼进 aria-labelledby 的 id 列表。
              const switchId = `${idPrefix}-group-${index}`;
              const toolNames = tools.map(({ name }) => name).join("、");
              return (
                <label
                  key={group}
                  htmlFor={switchId}
                  className={`flex cursor-pointer items-center gap-3 rounded-lg px-2.5 py-1.5 transition-colors hover:bg-muted/60 ${
                    disabled ? "cursor-not-allowed opacity-55" : ""
                  }`}
                >
                  <span className="min-w-0 flex-1">
                    <span id={`${switchId}-name`} className={`block truncate text-sm ${enabled ? "" : "text-muted-foreground"}`}>
                      {group}
                    </span>
                    <span id={`${switchId}-tools`} className="text-muted-foreground block truncate text-2xs" title={toolNames}>
                      {tools.length} 个工具：{toolNames}
                    </span>
                  </span>
                  <Switch
                    id={switchId}
                    checked={enabled}
                    aria-labelledby={`${switchId}-name`}
                    aria-describedby={`${switchId}-tools`}
                    disabled={disabled || !onDisabledToolGroupsChange}
                    onClick={() => setGroupEnabled(group, !enabled)}
                  />
                </label>
              );
            })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
};
