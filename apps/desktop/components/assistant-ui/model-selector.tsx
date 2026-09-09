"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { RefreshCwIcon, SparklesIcon } from "lucide-react";
import { useAui } from "@assistant-ui/react";

import {
  ModelSelectorContent as OfficialModelSelectorContent,
  ModelSelectorEffort as OfficialModelSelectorEffort,
  ModelSelectorList as OfficialModelSelectorList,
  ModelSelectorRoot as OfficialModelSelectorRoot,
  ModelSelectorSearch as OfficialModelSelectorSearch,
  ModelSelectorTrigger as OfficialModelSelectorTrigger,
  ModelSelectorValue as OfficialModelSelectorValue,
  type ModelOption,
  type ModelSelectorEffortOption,
} from "@/components/model-selector";
import { ProviderConfigPanel } from "@/components/assistant-ui/provider-config-panel";
import { useModelCatalog, type ModelCatalogModel } from "@/lib/model-catalog";
import {
  type ModelSelection,
  type ModelSelectionScope,
  getStoredSelectionSnapshot,
  subscribeStoredSelection,
  writeStoredSelection,
} from "@/lib/model-selection-storage";
import { cn } from "@/lib/utils";

const BACKEND_REASONING_EFFORTS: readonly ModelSelectorEffortOption[] = [
  { id: "low", name: "Low" },
  { id: "high", name: "High" },
  { id: "max", name: "Max" },
];

type ModelSelectorProps = {
  scope?: ModelSelectionScope;
  className?: string;
  onReadyChange?: (ready: boolean) => void;
  runtimeModelContext?: boolean;
};

type SelectionState = {
  scopeKey: string;
  selection: ModelSelection | null;
};

function scopeKey(scope?: ModelSelectionScope): string {
  return scope ? `${scope.kind}:${scope.id}` : "none";
}

function getSelection(
  models: readonly ModelCatalogModel[],
  stored: Partial<ModelSelection> | null,
): ModelSelection | null {
  const storedModel = stored
    ? models.find(
        (model) =>
          model.providerId === stored.providerId &&
          model.modelName === stored.modelName,
      )
    : undefined;
  const model = storedModel ?? models[0];
  if (!model) return null;

  return {
    providerId: model.providerId,
    modelName: model.modelName,
    reasoningEffort: model.supportsReasoningEffort
      ? stored?.reasoningEffort ?? "high"
      : null,
  };
}

function toOfficialModel(model: ModelCatalogModel): ModelOption {
  return {
    id: model.optionId,
    name: model.modelName,
    description: model.providerName,
    keywords: [model.providerName],
    efforts: model.supportsReasoningEffort
      ? BACKEND_REASONING_EFFORTS
      : undefined,
  };
}

function SelectorView({
  catalogModels,
  selection,
  className,
  onSelectionChange,
  onCatalogChanged,
}: {
  catalogModels: readonly ModelCatalogModel[];
  selection: ModelSelection;
  className?: string;
  onSelectionChange: (selection: ModelSelection) => void;
  onCatalogChanged: () => void;
}) {
  const officialModels = useMemo(
    () => catalogModels.map(toOfficialModel),
    [catalogModels],
  );
  const selectedModel = catalogModels.find(
    (model) =>
      model.providerId === selection.providerId &&
      model.modelName === selection.modelName,
  );
  const selectedOptionId = selectedModel?.optionId;

  if (!selectedModel || !selectedOptionId) return null;

  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-2", className)}>
      <OfficialModelSelectorRoot
        models={officialModels}
        value={selectedOptionId}
        effort={selection.reasoningEffort ?? undefined}
        onValueChange={(optionId) => {
          const model = catalogModels.find((candidate) => candidate.optionId === optionId);
          if (!model) return;
          onSelectionChange({
            providerId: model.providerId,
            modelName: model.modelName,
            reasoningEffort: model.supportsReasoningEffort
              ? selection.reasoningEffort ?? "high"
              : null,
          });
        }}
        onEffortChange={(reasoningEffort) => {
          if (!selectedModel.supportsReasoningEffort) return;
          onSelectionChange({
            ...selection,
            reasoningEffort: reasoningEffort as ModelSelection["reasoningEffort"],
          });
        }}
      >
        <OfficialModelSelectorTrigger
          variant="ghost"
          size="sm"
          className="max-w-60"
          aria-label="选择模型"
        >
          <SparklesIcon className="text-muted-foreground size-3.5 shrink-0" />
          <OfficialModelSelectorValue className="min-w-0" />
        </OfficialModelSelectorTrigger>
        <OfficialModelSelectorContent
          side="top"
          align="start"
          searchable
          className="w-72"
        >
          <OfficialModelSelectorSearch placeholder="搜索模型…" />
          <OfficialModelSelectorList />
          <OfficialModelSelectorEffort label="推理强度" />
          <div className="border-border/60 mt-1 border-t pt-1">
            <ProviderConfigPanel onChanged={onCatalogChanged} />
          </div>
        </OfficialModelSelectorContent>
      </OfficialModelSelectorRoot>
    </div>
  );
}

function useScopedSelection(scope: ModelSelectionScope | undefined) {
  const catalogState = useModelCatalog();
  const key = scopeKey(scope);
  const [unscopedSelection, setUnscopedSelection] = useState<ModelSelection | null>(null);
  const subscribe = useCallback(
    (listener: () => void) => (scope ? subscribeStoredSelection(scope, listener) : () => undefined),
    [key],
  );
  const getSnapshot = useCallback(
    () => (scope ? getStoredSelectionSnapshot(scope) : null),
    [key],
  );
  const storedSelection = useSyncExternalStore(subscribe, getSnapshot, () => null);
  const [selectionState, setSelectionState] = useState<SelectionState>({
    scopeKey: "",
    selection: null,
  });

  useEffect(() => {
    if (catalogState.status !== "ready" || !catalogState.catalog) return;
    const nextSelection = getSelection(
      catalogState.catalog.models,
      scope ? storedSelection : null,
    );
    setSelectionState({ scopeKey: key, selection: nextSelection });
    if (scope && nextSelection) writeStoredSelection(scope, nextSelection);
    if (!scope) setUnscopedSelection(nextSelection);
  }, [catalogState.catalog, catalogState.status, key, storedSelection]);

  const selection = scope
    ? catalogState.status === "ready" && catalogState.catalog
      ? getSelection(catalogState.catalog.models, storedSelection)
      : null
    : selectionState.scopeKey === key
      ? unscopedSelection ?? selectionState.selection
      : null;
  const updateSelection = (nextSelection: ModelSelection) => {
    setSelectionState({ scopeKey: key, selection: nextSelection });
    if (!scope) setUnscopedSelection(nextSelection);
    if (scope) writeStoredSelection(scope, nextSelection);
  };

  return { ...catalogState, selection, updateSelection };
}

function StandaloneModelSelector(props: Omit<ModelSelectorProps, "runtimeModelContext">) {
  const { catalog, status, retry, selection, updateSelection } = useScopedSelection(props.scope);

  useEffect(() => {
    props.onReadyChange?.(status === "ready" && selection !== null);
  }, [props.onReadyChange, selection, status]);

  if (status === "loading" || status === "idle") {
    return <span className="text-muted-foreground inline-flex h-8 items-center gap-1.5 px-2 text-xs"><SparklesIcon className="size-3.5 animate-pulse" />加载模型…</span>;
  }
  if (status === "error") {
    return <div className="flex items-center gap-1"><button type="button" onClick={() => void retry()} className="text-destructive hover:bg-destructive/10 inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs"><RefreshCwIcon className="size-3.5" />模型加载失败，重试</button><ProviderConfigPanel onChanged={() => void retry()} /></div>;
  }
  if (!catalog || !selection) {
    return <div className="flex items-center gap-1"><span className="text-muted-foreground inline-flex h-8 items-center px-2 text-xs">暂无可用模型</span><ProviderConfigPanel onChanged={() => void retry()} /></div>;
  }

  return <SelectorView catalogModels={catalog.models} selection={selection} className={props.className} onSelectionChange={updateSelection} onCatalogChanged={() => void retry()} />;
}

function RuntimeModelSelector(props: Omit<ModelSelectorProps, "runtimeModelContext">) {
  const aui = useAui();
  const { catalog, status, retry, selection, updateSelection } = useScopedSelection(props.scope);
  const selectedModel = catalog?.models.find(
    (model) => model.providerId === selection?.providerId && model.modelName === selection?.modelName,
  );

  useEffect(() => {
    if (!selectedModel || !selection) return;
    return aui.modelContext.register({
      getModelContext: () => ({
        config: {
          modelName: selectedModel.optionId,
          reasoningEffort: selection.reasoningEffort ?? undefined,
        },
      }),
    });
  }, [aui, selectedModel, selection]);

  useEffect(() => {
    props.onReadyChange?.(status === "ready" && selection !== null);
  }, [props.onReadyChange, selection, status]);

  if (status === "error") {
    return <div className="flex items-center gap-1"><button type="button" onClick={() => void retry()} className="text-destructive hover:bg-destructive/10 inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs"><RefreshCwIcon className="size-3.5" />模型加载失败，重试</button><ProviderConfigPanel onChanged={() => void retry()} /></div>;
  }
  if (status !== "ready" || !catalog || !selection) {
    return <span className="text-muted-foreground inline-flex h-8 items-center gap-1.5 px-2 text-xs"><SparklesIcon className="size-3.5 animate-pulse" />加载模型…</span>;
  }

  return <SelectorView catalogModels={catalog.models} selection={selection} className={props.className} onSelectionChange={updateSelection} onCatalogChanged={() => void retry()} />;
}

export function ModelSelector(props: ModelSelectorProps) {
  if (props.runtimeModelContext) return <RuntimeModelSelector {...props} />;
  return <StandaloneModelSelector {...props} />;
}
