/**
 * 模型选择器组件（TaskHeaderBar 内，NewTaskPage / ChatPanel 顶部共用）。
 *
 * 基于 cmdk（shadcn Command 底座）+ Popover 的搜索式选择器（设计 §10.1）：
 * - 按厂商分组展示可用模型（数据源 taskStore.availableModels，来自 GET /models 唯一数据源）；
 * - 行内展示美化后的模型路由名 + 厂商 badge + 能力徽标（思考/图片/视频）；
 * - 键盘优先：↑↓ 导航、输入即过滤、Enter 选中；
 * - 未选择（selectedModel=null）时折叠态显示「选择模型」引导显式选择，
 *   模型必须显式选择（产品已移除「Auto 跟随 Agent 默认」语义）；
 * - 仅当模型声明支持推理强度（model.reasoning_effort?.supported === true）时，
 *   在该模型行内渲染紧凑档位选择行（档位来自 effort_map 的键，使用厂商原始键名，
 *   如 deepseek 的 low/high/max），选中档位经 taskStore.setSelectedReasoningEffort
 *   持久化；不支持强度的模型完全不渲染档位控件（不灰显、不占噪）；
 * - 折叠态标签在已选模型支持强度且存在选中档位时追加轻量后缀
 *   （美化名 + "·" + selectedReasoningEffort）；
 * - 空态引导「暂无可用模型，点击配置」+ 底部 footer「配置模型 / 管理厂商」，
 *   两者均回调 onOpenSettings 打开 ProviderSettingsDialog。
 *
 * 契约对齐（§3.4）：GET /models 是已配置厂商的可用模型**唯一且充分的体验数据源**。
 * 后端返回字段中不含窗口大小（max_context_window 未被端点投影），前端**诚实省略**
 * 窗口徽标（不显示伪造 0、不显示占位），属「信息不存在则不说」的诚实设计。
 * 视觉权重低于输入框（h-7 紧凑风格，与其他顶部控件对齐）。
 *
 * @module components/chat/ModelSelector
 */

import { useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Cpu, Image as ImageIcon, Settings2, Sparkles, Video } from "lucide-react";
import type { ModelEntryRecord } from "@shared/model";
import { findModelBySelection, isModelSelection } from "@shared/model";
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
 * 去掉模型路由名前缀，得到可读的展示基线（如 `deepseek/deepseek-v4-flash` → `deepseek-v4-flash`）。
 *
 * 后端 model_name 是注册表纯净名（全局唯一，不含厂商前缀的也直接是裸名），
 * 形如 `provider/model-id` 时去前缀更易读；无 `/` 的裸名原样返回。
 *
 * @param modelName - 后端模型路由名（如 deepseek-v4-flash）。
 * @returns 去前缀后的可读展示基线。
 */
function stripProviderPrefix(modelName: string): string {
  const idx = modelName.indexOf("/");
  return idx >= 0 ? modelName.slice(idx + 1) : modelName;
}

/**
 * 计算每模型应展示的标签（跨厂商重名兜底）。
 *
 * GET /models 的 model_name 全局唯一，但去前缀后的可读名可能跨厂商重名
 * （如 `openai/gpt-4o` 与 `azure/gpt-4o` 去前缀均为 `gpt-4o`）。
 * 此时回退展示**完整 model_name**（带前缀），保证可读且可区分，
 * 避免两行标签相同致用户无法抉择（§3.4 跨厂商重名兜底）。
 *
 * @param models - 当前可用模型列表。
 * @returns model_name → 展示标签 的映射。
 */
function buildDisplayLabels(models: ModelEntryRecord[]): Map<string, string> {
  const strippedCounts = new Map<string, number>();
  for (const model of models) {
    const base = stripProviderPrefix(model.model_name);
    strippedCounts.set(base, (strippedCounts.get(base) ?? 0) + 1);
  }
  const labels = new Map<string, string>();
  for (const model of models) {
    const base = stripProviderPrefix(model.model_name);
    labels.set(model.model_name, strippedCounts.get(base) === 1 ? base : model.model_name);
  }
  return labels;
}

/**
 * 模型下拉选择器。
 *
 * 折叠态为单行 `图标 标签 ∨`；展开后为可搜索的分组列表。选中态经
 * taskStore.setSelectedModel 持久化（入参为完整模型条目，二元组由 store 统一抽取）；
 * 未选择（null）时折叠态显示「选择模型」占位，引导用户显式选择（产品已移除 Auto 语义）。
 * 打开下拉时幂等刷新一次模型缓存，保证下拉即时反映配置中心变更（§9.5）。
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
 * - 选中变更经 taskStore.setSelectedModel 持久化（localStorage）。
 */
export function ModelSelector({ onOpenSettings, className }: ModelSelectorProps) {
  const [open, setOpen] = useState(false);
  const selectedModel = useTaskStore((s) => s.selectedModel);
  const selectedReasoningEffort = useTaskStore((s) => s.selectedReasoningEffort);
  const availableModels = useTaskStore((s) => s.availableModels);
  const setSelectedModel = useTaskStore((s) => s.setSelectedModel);
  const setSelectedReasoningEffort = useTaskStore((s) => s.setSelectedReasoningEffort);
  const refreshAvailableModels = useTaskStore((s) => s.refreshAvailableModels);

  // 打开下拉时刷新缓存：配置中心可能刚保存/删除，保证下拉与校验数据新鲜。
  useEffect(() => {
    if (open) {
      void refreshAvailableModels();
    }
  }, [open, refreshAvailableModels]);

  // 展示标签映射（含跨厂商重名兜底）。
  const displayLabels = useMemo(() => buildDisplayLabels(availableModels), [availableModels]);

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

  /** 折叠态标签：显式模型取去前缀美化名；未选择时显示「选择模型」引导。 */
  const currentLabel = useMemo(() => {
    if (selectedModel === null) return "选择模型";
    const label = displayLabels.get(selectedModel.model_name) ?? selectedModel.model_name;
    // 仅当当前选中模型支持强度且存在已选档位时，追加轻量后缀（美化名·档位），
    // 与 selectorLabel(max-w-120px) truncate 一致，不额外占宽。
    // 走共享的二元组匹配，避免跨厂商重名取到错误的能力信息。
    const currentModel = findModelBySelection(availableModels, selectedModel);
    if (selectedReasoningEffort && currentModel?.reasoning_effort?.supported === true) {
      return `${label}·${selectedReasoningEffort}`;
    }
    return label;
  }, [availableModels, displayLabels, selectedModel, selectedReasoningEffort]);

  /**
   * 选中处理：写入完整模型条目（产品已移除 Auto，不再写入 null）。
   *
   * 传入条目而非裸 model_name：store 需同时拿到 provider_id 才能满足后端创建轮次
   * 的配对契约，由调用点保证二元组完整。
   *
   * 注意：cmdk 的 onSelect 回调参数是 item 的 `value` 字符串（本组件为
   * 「展示标签 + model_name + providerName」组合，供搜索匹配），不能
   * 直接写入 store——必须在调用点用闭包传入真实模型条目。
   */
  const handleSelect = (model: ModelEntryRecord) => {
    setSelectedModel(model);
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
          {/* 选择器折叠态标签净空 120px（紧凑态固定宽度，非通用语义） */}
          <span className="max-w-selectorLabel truncate">{currentLabel}</span>
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
                  const isSelected = isModelSelection(model, selectedModel);
                  const effortKeys =
                    model.reasoning_effort?.supported === true
                      ? Object.keys(model.reasoning_effort.effort_map)
                      : [];
                  // 未启用或未配 Key 的模型：行内 muted 样式 + 角标，引导去厂商配置，
                  // 而非直接隐藏（配置清单层可见、模型选择层可能不现属预期分层，详见 §3.5）。
                  const isDisabled = !model.enabled || !model.api_key_configured;
                  return (
                    <div key={model.model_name}>
                      <CommandItem
                        value={`${displayLabels.get(model.model_name) ?? model.model_name} ${model.model_name} ${providerName}`}
                        onSelect={() => handleSelect(model)}
                        className={cn("text-xs", isDisabled && "opacity-60")}
                      >
                        <span className={cn("flex-1 truncate", isDisabled && "text-muted-foreground")}>
                          {displayLabels.get(model.model_name) ?? model.model_name}
                        </span>
                        {/* 能力徽标：后端已返回，新增展示，体验增强（§3.4）。
                         * 支持思考 / 图片 / 视频分别用不同图标表达。 */}
                        {model.supports_thinking && (
                          <Sparkles
                            className="h-3 w-3 shrink-0 text-amber-500"
                            aria-label="支持思考"
                          />
                        )}
                        {model.supports_image && (
                          <ImageIcon
                            className="h-3 w-3 shrink-0 text-sky-500"
                            aria-label="支持图片输入"
                          />
                        )}
                        {model.supports_video && (
                          <Video
                            className="h-3 w-3 shrink-0 text-violet-500"
                            aria-label="支持视频输入"
                          />
                        )}
                        {isDisabled && (
                          <span className={cn("shrink-0", Caption.xs10, "text-amber-500")}>
                            {model.enabled ? "未配 Key" : "未启用"}
                          </span>
                        )}
                        {isSelected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                      </CommandItem>
                      {/* 仅当模型支持推理强度时渲染档位选择行：紧凑 text-xs，与徽标语言一致，
                       * 不另起视觉体系；不支持强度的模型完全不渲染（不灰显、不占噪）。
                       * 档位使用 effort_map 原始键（厂商语义，如 low/high/max），不前端硬编码统一档位名。
                       * 点击档位调用 setSelectedReasoningEffort(key)，当前选中档位以 text-primary 高亮。 */}
                      {effortKeys.length > 0 && (
                        <div
                          className="flex flex-wrap items-center gap-1 px-2 pb-1.5 pt-0.5"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <span className={cn("shrink-0", Caption.xs10, "text-muted-foreground")}>
                            强度
                          </span>
                          {effortKeys.map((key) => {
                            const active = selectedReasoningEffort === key;
                            return (
                              <button
                                key={key}
                                type="button"
                                aria-pressed={active}
                                onClick={() => setSelectedReasoningEffort(key)}
                                className={cn(
                                  "rounded px-1.5 py-0.5 text-xs transition-colors",
                                  active
                                    ? "bg-accent font-medium text-primary"
                                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                                )}
                              >
                                {key}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>
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
