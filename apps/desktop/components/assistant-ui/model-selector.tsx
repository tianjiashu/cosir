"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  RefreshCwIcon,
  SparklesIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import {
  getModelGroups,
  type ModelListItem,
  type ProviderModelGroup,
} from "@/lib/api/models";
import { ReasoningEffortSelect } from "@/components/assistant-ui/reasoning-effort-select";
import { ProviderConfigPanel } from "@/components/assistant-ui/provider-config-panel";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  type ModelSelection,
  parseStoredSelection,
  readStoredSelection,
  selectionStorageKey,
  writeStoredSelection,
} from "@/lib/model-selection-storage";

function findModel(
  groups: ProviderModelGroup[],
  providerId: number,
  modelName: string,
): ModelListItem | undefined {
  return groups
    .find((group) => group.provider_id === providerId)
    ?.models.find((model) => model.model_name === modelName);
}

export function ModelSelector({ taskId, className, onReadyChange }: { taskId?: number; className?: string; onReadyChange?: (ready: boolean) => void }) {
  const [groups, setGroups] = useState<ProviderModelGroup[]>([]);
  const [selection, setSelection] = useState<ModelSelection | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const loadModels = useCallback(async () => {
    setStatus("loading");
    try {
      const nextGroups = await getModelGroups();
      setGroups(nextGroups);
      setStatus("ready");

      const storageKey = taskId === undefined ? null : selectionStorageKey(taskId);
      const stored = storageKey ? window.localStorage.getItem(storageKey) : null;
      const storedSelection = taskId === undefined ? null : readStoredSelection(taskId);
      if (storageKey && stored && !parseStoredSelection(stored)) window.localStorage.removeItem(storageKey);
      const storedModel = storedSelection
        ? findModel(nextGroups, Number(storedSelection.providerId), String(storedSelection.modelName))
        : undefined;
      const firstGroup = nextGroups.find((group) => group.models.length > 0);
      const firstModel = firstGroup?.models[0];
      const providerId = storedModel ? Number(storedSelection?.providerId) : firstGroup?.provider_id;
      const model = storedModel ?? firstModel;

      if (providerId !== undefined && model) {
        const nextSelection: ModelSelection = {
          providerId,
          modelName: model.model_name,
            reasoningEffort: storedModel && storedSelection?.reasoningEffort
              ? storedSelection.reasoningEffort
              : model.supports_reasoning_effort ? "high" : null,
        };
        setSelection(nextSelection);
        if (storageKey) writeStoredSelection(taskId!, nextSelection);
      } else {
        setSelection(null);
      }
    } catch {
      setStatus("error");
    }
  }, [taskId]);

  useEffect(() => {
    onReadyChange?.(status === "ready" && Boolean(selection));
  }, [onReadyChange, selection, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadModels(), 0);
    return () => window.clearTimeout(timer);
  }, [loadModels]);

  const selectedModel = useMemo(
    () => selection ? findModel(groups, selection.providerId, selection.modelName) : undefined,
    [groups, selection],
  );
  const selectedProvider = groups.find((group) => group.provider_id === selection?.providerId);
  const hasModels = groups.some((group) => group.models.length > 0);
  const modelOptions = useMemo(
    () => groups.flatMap((group) => group.models.map((model) => ({ group, model }))),
    [groups],
  );

  useEffect(() => {
    if (open) optionRefs.current[activeIndex]?.focus();
  }, [activeIndex, open]);

  const openModelMenu = () => {
    const selectedIndex = modelOptions.findIndex(
      ({ group, model }) => group.provider_id === selection?.providerId && model.model_name === selection?.modelName,
    );
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    setOpen(true);
  };

  const updateSelection = (next: ModelSelection) => {
    setSelection(next);
    if (taskId !== undefined) {
      writeStoredSelection(taskId, next);
    }
    setOpen(false);
  };

  if (status === "loading") {
    return (
      <span className="text-muted-foreground inline-flex h-8 items-center gap-1.5 px-2 text-xs">
        <SparklesIcon className="size-3.5 animate-pulse" />
        加载模型…
      </span>
    );
  }

  if (status === "error") {
    return (
      <div className="flex items-center gap-1">
        <button type="button" onClick={() => void loadModels()} className="text-destructive hover:bg-destructive/10 inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs">
          <RefreshCwIcon className="size-3.5" />
          模型加载失败，重试
        </button>
        <ProviderConfigPanel onChanged={() => void loadModels()} />
      </div>
    );
  }

  if (!hasModels || !selection || !selectedModel || !selectedProvider) {
    return (
      <div className="flex items-center gap-1">
        <span className="text-muted-foreground inline-flex h-8 items-center px-2 text-xs">暂无可用模型</span>
        <ProviderConfigPanel onChanged={() => void loadModels()} />
      </div>
    );
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <Popover open={open} onOpenChange={(nextOpen) => {
        if (nextOpen) openModelMenu();
        else setOpen(false);
      }}>
        <PopoverTrigger
          type="button"
          aria-expanded={open}
          aria-haspopup="menu"
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              openModelMenu();
            }
          }}
          className="bg-transparent text-foreground inline-flex h-8 max-w-60 items-center gap-1.5 rounded-md px-2 text-xs outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring/50"
        >
          <SparklesIcon className="text-muted-foreground size-3.5 shrink-0" />
          <span className="truncate font-medium">{selection.modelName}</span>
          <ChevronDownIcon className={cn("text-muted-foreground size-3 shrink-0 transition-transform", open && "rotate-180")} />
        </PopoverTrigger>
        <PopoverContent side="top" align="start" keepMounted className="w-72 p-2">
          <p className="text-muted-foreground px-2 pb-1.5 text-[11px] font-medium uppercase tracking-wide">选择模型</p>
          <div role="menu" aria-label="模型列表" className="max-h-72 overflow-y-auto">
            {groups.map((group) => (
              <div key={group.provider_id} className="pb-1 last:pb-0">
                <p className="text-muted-foreground px-2 py-1 text-[11px] font-medium">{group.provider_name}</p>
                {group.models.map((model) => {
                  const optionIndex = modelOptions.findIndex(
                    ({ group: optionGroup, model: optionModel }) => optionGroup.provider_id === group.provider_id && optionModel.model_name === model.model_name,
                  );
                  const isSelected = selection.providerId === group.provider_id && selection.modelName === model.model_name;
                  return (
                    <button
                      key={`${group.provider_id}:${model.model_name}`}
                      type="button"
                      role="menuitemradio"
                      aria-checked={isSelected}
                      tabIndex={activeIndex === optionIndex ? 0 : -1}
                      ref={(element) => { optionRefs.current[optionIndex] = element; }}
                      onKeyDown={(event) => {
                        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                          event.preventDefault();
                          const direction = event.key === "ArrowDown" ? 1 : -1;
                          setActiveIndex((optionIndex + direction + modelOptions.length) % modelOptions.length);
                        }
                        if (event.key === "Home" || event.key === "End") {
                          event.preventDefault();
                          setActiveIndex(event.key === "Home" ? 0 : modelOptions.length - 1);
                        }
                      }}
                      onClick={() => updateSelection({
                        providerId: group.provider_id,
                        modelName: model.model_name,
                        reasoningEffort: model.supports_reasoning_effort ? selection.reasoningEffort ?? "high" : null,
                      })}
                      className="hover:bg-muted flex w-full items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-sm outline-none focus-visible:bg-muted"
                    >
                      <span className="truncate">{model.model_name}</span>
                      {isSelected && <CheckIcon className="text-primary size-4 shrink-0" />}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <div className="border-border/60 mt-1 border-t pt-1">
            <ProviderConfigPanel onBeforeOpen={() => setOpen(false)} onChanged={() => void loadModels()} />
          </div>
        </PopoverContent>
      </Popover>

      {selectedModel.supports_reasoning_effort && (
        <ReasoningEffortSelect
          value={selection.reasoningEffort ?? "high"}
          onValueChange={(reasoningEffort) => updateSelection({ ...selection, reasoningEffort })}
        />
      )}
    </div>
  );
}
