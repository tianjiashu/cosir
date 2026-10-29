import { useId, type FC } from "react";
import { WrenchIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EmptyState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { COMPOSER_ICON_BUTTON_CLASS_NAME } from "@/components/composer/composer-surface";
import type { ToolGroupCatalog } from "@/lib/api/tools";

type ToolGroupSelectorProps = {
  toolGroups: ToolGroupCatalog[];
  /**
   * 被关闭（禁用）的工具组。名称沿用上游 ComposerPromptToolbar 的 props；发送命令与 Run 冻结的
   * `disabledToolGroups` 都以禁用的组名为契约，界面只是反过来呈现成开关。
   */
  selectedToolGroups: string[];
  onSelectedToolGroupsChange?: (groups: string[]) => void;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
  /** 只读原因（如运行期配置锁定）：面板仍可打开查看，开关不可改，并显示这条说明。 */
  lockedHint?: string;
};

/**
 * 本轮对话可用工具组的开关面板。
 *
 * 用户心智是「开着哪些工具」，因此开关为开表示可用；数据仍是禁用的组名列表（后端
 * `disabledToolGroups` 契约），在这里做一次取反。触发按钮只在有组被关闭时提示数量，
 * 默认全开时保持安静。
 */
export const ToolGroupSelector: FC<ToolGroupSelectorProps> = ({
  toolGroups,
  selectedToolGroups: disabledToolGroups,
  onSelectedToolGroupsChange: onDisabledToolGroupsChange,
  loading = false,
  error,
  disabled = false,
  lockedHint,
}) => {
  const idPrefix = useId();
  const disabledSet = new Set(disabledToolGroups);
  const disabledCount = toolGroups.filter(({ group }) => disabledSet.has(group)).length;
  const enabledCount = toolGroups.length - disabledCount;
  const canEdit = Boolean(onDisabledToolGroupsChange)
    && lockedHint === undefined
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
        className={COMPOSER_ICON_BUTTON_CLASS_NAME}
        aria-label={disabledCount > 0 ? `工具，已关闭 ${disabledCount} 组` : "工具"}
        title={disabledCount > 0 ? `工具（已关闭 ${disabledCount} 组）` : "工具"}
      >
        <WrenchIcon />
        {/* 有工具组被关闭时只用角标提醒，数量见 title 与弹层头部。 */}
        {disabledCount > 0 && (
          <span aria-hidden="true" className="bg-warning ring-card absolute top-0.5 right-0.5 size-2 rounded-full ring-2" />
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
        {lockedHint && <p className="text-muted-foreground px-1 pt-2 text-2xs">{lockedHint}</p>}
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
                    disabled={!canEdit}
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
