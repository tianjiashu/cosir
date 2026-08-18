/**
 * 模型选择器组件（TaskHeaderBar 内，NewTaskPage / ChatPanel 顶部共用）。
 *
 * 基于 cmdk（shadcn Command 底座）+ Popover 的搜索式选择器（设计 §10.1）：
 * - 按厂商分组展示可用模型（数据源 taskStore.availableModels）；
 * - 行内展示 display_name + 厂商 badge + 上下文窗口缩写（128k/1M）+ thinking 徽标；
 * - 键盘优先：↑↓ 导航、输入即过滤、Enter 选中；
 * - 未选择（selectedModelName=null）时折叠态显示「选择模型」引导显式选择，
 *   模型必须显式选择（产品已移除「Auto 跟随 Agent 默认」语义）；
 * - 空态引导「暂无可用模型，点击配置」+ 底部 footer「配置模型 / 管理厂商」，
 *   两者均回调 onOpenSettings 打开 ProviderSettingsDialog。
 *
 * 视觉权重低于输入框（h-7 紧凑风格，与 AgentSelector 对齐）。
 *
 * @module components/chat/ModelSelector
 */

import { useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Cpu, Settings2, Sparkles } from "lucide-react";
import type { ModelEntryRecord } from "@shared/model";
import { cn } from "@/lib/utils";
import { useTaskStore } from "@/stores/taskStore";
import { Caption } from "@/components/ui/tokens";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";

/** ModelSelector 组件属性。 */
interface ModelSelectorProps {
  /** 打开模型厂商配置中心的回调（footer 与空态引导共用）。 */
  onOpenSettings: () => void;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * 将上下文窗口 token 数格式化为紧凑缩写（如 131072 → 128k，2000000 → 2M）。
 *
 * @param tokens - 上下文窗口 token 数。
 * @returns 紧凑缩写文本；无效值返回空字符串（不展示该徽标）。
 */
function formatContextWindow(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return "";
  if (tokens >= 1_000_000) return `${Math.round(tokens / 1_000_000)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return `${tokens}`;
}

/**
 * 模型下拉选择器。
 *
 * 折叠态为单行 `图标 标签 ∨`；展开后为可搜索的分组列表。选中态经
 * taskStore.setSelectedModelName 持久化；未选择（null）时折叠态显示
 * 「选择模型」占位，引导用户显式选择（产品已移除 Auto 语义）。打开
 * 下拉时幂等刷新一次模型缓存，保证下拉即时反映配置中心变更（§9.5）。
 *
 * @param props - 组件属性。
 * @param props.onOpenSettings - 「配置模型 / 管理厂商」入口回调（由宿主持有
 *   ProviderSettingsDialog 开关，本组件不感知对话框生命周期）。
 * @param props.className - 可选的外层类名注入。
 * @returns ModelSelector 的 React 元素。
 *
 * @sideeffect
 * - 打开下拉时调用 taskStore.refreshAvailableModels 幂等刷新模型缓存
 *   （`GET /models`，失败降级为空列表，不阻塞渲染）。
 * - 选中变更经 taskStore.setSelectedModelName 持久化（localStorage）。
 */
export function ModelSelector({ onOpenSettings, className }: ModelSelectorProps) {
  const [open, setOpen] = useState(false);
  const selectedModelName = useTaskStore((s) => s.selectedModelName);
  const availableModels = useTaskStore((s) => s.availableModels);
  const setSelectedModelName = useTaskStore((s) => s.setSelectedModelName);
  const refreshAvailableModels = useTaskStore((s) => s.refreshAvailableModels);

  // 打开下拉时刷新缓存：配置中心可能刚保存/删除，保证下拉与校验数据新鲜。
  useEffect(() => {
    if (open) {
      void refreshAvailableModels();
    }
  }, [open, refreshAvailableModels]);

  // 按厂商分组（保持后端返回顺序：已按 sort_order 排序）。
  const grouped = useMemo(() => {
    const groups = new Map<string, ModelEntryRecord[]>();
    for (const model of availableModels) {
      const list = groups.get(model.provider_name);
      if (list) {
        list.push(model);
      } else {
        groups.set(model.provider_name, [model]);
      }
    }
    return [...groups.entries()];
  }, [availableModels]);

  /** 折叠态标签：显式模型取 display_name；未选择时显示「选择模型」引导。 */
  const currentLabel = useMemo(() => {
    if (selectedModelName === null) return "选择模型";
    return (
      availableModels.find((model) => model.model_name === selectedModelName)?.display_name ??
      selectedModelName
    );
  }, [availableModels, selectedModelName]);

  /**
   * 选中处理：写入 litellm 路由名（产品已移除 Auto，不再写入 null）。
   *
   * 注意：cmdk 的 onSelect 回调参数是 item 的 `value` 字符串（本组件为
   * 「display_name + model_name + providerName」组合，供搜索匹配），不能
   * 直接写入 store——必须在调用点用闭包传入真实 model_name。
   */
  const handleSelect = (modelName: string) => {
    setSelectedModelName(modelName);
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="选择模型"
          className={cn(
            "inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium text-muted-foreground hover:bg-accent transition-colors",
            className,
          )}
        >
          <Cpu className="h-3.5 w-3.5 shrink-0" />
          {/* 选择器折叠态标签净空 120px（与 AgentSelector 的 80px 同为紧凑态固定宽度，非通用语义） */}
          {/* eslint-disable-next-line tailwind/no-arbitrary-value */}
          <span className="max-w-[120px] truncate">{currentLabel}</span>
          <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>

      <PopoverContent align="start" className="w-72 p-0">
        <Command>
          <CommandInput placeholder="搜索模型..." className="h-8 text-xs" />
          <CommandList className="max-h-72">
            {availableModels.length === 0 ? (
              /* 空态引导：无任何可用模型，直达配置中心（设计 §10.1）。
               * 此时不渲染 CommandEmpty（cmdk 的 Empty 在「过滤后无 item」时
               * 也会出现，与引导按钮并存会造成语义冗余）。 */
              <div className="flex justify-center px-2 py-1">
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onOpenSettings();
                  }}
                  className="flex items-center gap-1.5 rounded px-2 py-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Settings2 className="h-3.5 w-3.5" />
                  暂无可用模型，点击配置
                </button>
              </div>
            ) : (
              /* 有模型但搜索过滤无结果时才提示「未找到匹配的模型」。 */
              <CommandEmpty>
                <span className="text-xs text-muted-foreground">未找到匹配的模型</span>
              </CommandEmpty>
            )}

            {/* 按厂商分组 */}
            {grouped.map(([providerName, models]) => (
              <CommandGroup key={providerName} heading={providerName}>
                {models.map((model) => {
                  const isSelected = model.model_name === selectedModelName;
                  return (
                    <CommandItem
                      key={model.model_id}
                      value={`${model.display_name} ${model.model_name} ${providerName}`}
                      onSelect={() => handleSelect(model.model_name)}
                      className="text-xs"
                    >
                      <span className="flex-1 truncate">{model.display_name}</span>
                      <span className={cn("shrink-0", Caption.xs10, "text-muted-foreground")}>
                        {formatContextWindow(model.max_context_window)}
                      </span>
                      {model.supports_thinking && (
                        <Sparkles
                          className="h-3 w-3 shrink-0 text-amber-500"
                          aria-label="支持思考"
                        />
                      )}
                      {isSelected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            ))}
          </CommandList>

          {/* 底部固定 footer：直达配置中心 */}
          <CommandSeparator />
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onOpenSettings();
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <Settings2 className="h-3.5 w-3.5" />
            配置模型 / 管理厂商
          </button>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
