"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { SparklesIcon } from "lucide-react";
import { useAui } from "@assistant-ui/react";

import {
  ModelSelectorContent as OfficialModelSelectorContent,
  ModelSelectorEffort as OfficialModelSelectorEffort,
  ModelSelectorList as OfficialModelSelectorList,
  ModelSelectorRoot as OfficialModelSelectorRoot,
  ModelSelectorSearch as OfficialModelSelectorSearch,
  ModelSelectorTrigger as OfficialModelSelectorTrigger,
  ModelSelectorValue as OfficialModelSelectorValue,
  DEFAULT_EFFORT_OPTIONS as BACKEND_REASONING_EFFORTS,
  type ModelOption,
} from "@/components/model-selector";
import { ModelAvailability } from "@/components/model/model-availability";
import { ModelConfigPanel } from "@/components/assistant-ui/model-config-panel";
import type { TransportRunLaunch } from "@/lib/assistant/contract";
import { RUN_LAUNCH_LOCKED_HINT } from "@/lib/assistant/run-launch";
import { useModelCatalog, type ModelCatalogModel } from "@/lib/model-catalog";
import {
  scopeSuffix,
  type ModelSelection,
  type ModelSelectionScope,
  getStoredSelectionSnapshot,
  subscribeStoredSelection,
  writeStoredSelection,
} from "@/lib/model-selection-storage";

type ModelSelectorProps = {
  scope?: ModelSelectionScope;
  onReadyChange?: (ready: boolean) => void;
  runtimeModelContext?: boolean;
  /**
   * 运行期配置锁定：传入时显示这份 Run 冻结的配置且不可更改，也不写回存储的偏好；停止后恢复为
   * 用户偏好。
   */
  locked?: TransportRunLaunch | null;
};

type SelectionState = {
  scopeKey: string;
  selection: ModelSelection | null;
};

function scopeKey(scope?: ModelSelectionScope): string {
  return scope ? scopeSuffix(scope) : "none";
}

function getSelection(
  models: readonly ModelCatalogModel[],
  stored: Partial<ModelSelection> | null,
): ModelSelection | null {
  const storedModel = stored
    ? models.find(
        (model) =>
          model.modelConfigId === stored.modelConfigId,
      )
    : undefined;
  const model = storedModel ?? models[0];
  if (!model) return null;

  return {
    modelConfigId: model.modelConfigId,
    reasoningEffort: model.supportsReasoningEffort
      ? stored?.reasoningEffort ?? "high"
      : null,
  };
}

function toOfficialModel(model: ModelCatalogModel): ModelOption {
  return {
    id: model.optionId,
    name: model.label,
    triggerName: model.modelName,
    keywords: [model.configName, model.modelName],
    efforts: model.supportsReasoningEffort
      ? BACKEND_REASONING_EFFORTS
      : undefined,
  };
}

function SelectorView({
  catalogModels,
  selection,
  disabled = false,
  onSelectionChange,
}: {
  catalogModels: readonly ModelCatalogModel[];
  selection: ModelSelection;
  disabled?: boolean;
  onSelectionChange: (selection: ModelSelection) => void;
}) {
  const officialModels = useMemo(
    () => catalogModels.map(toOfficialModel),
    [catalogModels],
  );
  const selectedModel = catalogModels.find(
    (model) =>
      model.modelConfigId === selection.modelConfigId,
  );
  const selectedOptionId = selectedModel?.optionId;

  if (!selectedModel || !selectedOptionId) return null;

  return (
    <div
      className="flex min-w-0 max-w-full flex-wrap items-center gap-2"
      title={disabled ? RUN_LAUNCH_LOCKED_HINT : undefined}
    >
      <OfficialModelSelectorRoot
        models={officialModels}
        value={selectedOptionId}
        effort={selection.reasoningEffort ?? undefined}
        onValueChange={(optionId) => {
          const model = catalogModels.find((candidate) => candidate.optionId === optionId);
          if (!model) return;
          onSelectionChange({
            modelConfigId: model.modelConfigId,
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
          className="min-w-0 max-w-full sm:max-w-60"
          aria-label="选择模型"
          disabled={disabled}
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
            <ModelConfigPanel />
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

/** 锁定配置转成选择器的选择；模型配置已删除时为 null。 */
function lockedSelection(launch: TransportRunLaunch): ModelSelection | null {
  if (launch.modelConfigId === null) return null;
  return {
    modelConfigId: launch.modelConfigId,
    reasoningEffort: launch.reasoningEffort as ModelSelection["reasoningEffort"],
  };
}

/** 偏好选择或锁定配置，二者择一；锁定时 `locked` 为 true，界面只读。 */
function useEffectiveSelection(props: Omit<ModelSelectorProps, "runtimeModelContext">) {
  const scoped = useScopedSelection(props.scope);
  const locked = props.locked ?? null;
  const selection = locked === null ? scoped.selection : lockedSelection(locked);
  const selectedModel = scoped.catalog?.models.find(
    (model) => model.modelConfigId === selection?.modelConfigId,
  );
  return { ...scoped, selection, selectedModel, locked: locked !== null };
}

function SelectorBody({
  props,
  state,
}: {
  props: Omit<ModelSelectorProps, "runtimeModelContext">;
  state: ReturnType<typeof useEffectiveSelection>;
}) {
  const { catalog, status, retry, selection, selectedModel, updateSelection, locked } = state;

  useEffect(() => {
    props.onReadyChange?.(status === "ready" && selection !== null);
  }, [props.onReadyChange, selection, status]);

  const configure = <ModelConfigPanel />;
  return (
    <ModelAvailability status={status} modelCount={catalog?.models.length ?? 0} onRetry={retry} configure={configure}>
      {locked && !selectedModel && (
        <span className="text-muted-foreground text-xs">运行中的模型配置已删除，停止后重新选择</span>
      )}
      {selection && catalog && (
        <SelectorView catalogModels={catalog.models} selection={selection} disabled={locked} onSelectionChange={updateSelection} />
      )}
    </ModelAvailability>
  );
}

function StandaloneModelSelector(props: Omit<ModelSelectorProps, "runtimeModelContext">) {
  return <SelectorBody props={props} state={useEffectiveSelection(props)} />;
}

function RuntimeModelSelector(props: Omit<ModelSelectorProps, "runtimeModelContext">) {
  const aui = useAui();
  const state = useEffectiveSelection(props);
  const { selection, selectedModel } = state;

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

  return <SelectorBody props={props} state={state} />;
}

export function ModelSelector(props: ModelSelectorProps) {
  if (props.runtimeModelContext) return <RuntimeModelSelector {...props} />;
  return <StandaloneModelSelector {...props} />;
}
