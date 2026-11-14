import { useMemo } from "react";
import { SparklesIcon } from "lucide-react";
import { useAuiState } from "@assistant-ui/react";

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
} from "@/components/model/model-selector";
import { ModelAvailability } from "@/components/model/model-availability";
import type { TransportRunLaunch } from "@/lib/assistant/contract";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { RUN_LAUNCH_LOCKED_HINT } from "@/lib/assistant/run-launch";
import { useModelCatalog, type ModelCatalogModel } from "@/lib/model/catalog";
import {
  resolveModelSelection,
  selectionFor,
  useModelSelectionStore,
  type ModelSelection,
  type ModelSelectionScope,
  type SessionLaunchFacts,
} from "@/lib/model/model-selection-store";

type ModelSelectorProps = {
  scope: ModelSelectionScope;
  /**
   * 运行期配置锁定：传入时显示这份 Run 冻结的配置且不可更改，也不写回偏好；停止后恢复按回落链显示。
   */
  locked: TransportRunLaunch | null;
};

function toOfficialModel(model: ModelCatalogModel): ModelOption {
  return {
    id: model.optionId,
    name: model.modelName,
    group: model.connectionName,
    keywords: [model.connectionName],
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
          onSelectionChange(selectionFor(model, selection.reasoningEffort));
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
          className="min-w-0 max-w-full @lg:max-w-60"
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
        </OfficialModelSelectorContent>
      </OfficialModelSelectorRoot>
    </div>
  );
}

/** 会话快照里参与回落的两项发起配置（见 {@link resolveModelSelection}）。 */
function useSessionLaunchFacts(): SessionLaunchFacts {
  const lastLaunch = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.activity?.launch ?? null);
  const inheritedLaunch = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.fork?.initialLaunch ?? null);
  return { lastLaunch, inheritedLaunch };
}

const DELETED_LOCKED_MODEL_HINT = "运行中的模型配置已删除，停止后重新选择";

/** 锁定配置转成选择器的选择；模型配置已删除时为 null。 */
function lockedSelection(launch: TransportRunLaunch): ModelSelection | null {
  if (launch.modelConfigId === null) return null;
  return {
    modelConfigId: launch.modelConfigId,
    reasoningEffort: launch.reasoningEffort as ModelSelection["reasoningEffort"],
  };
}

/**
 * 会话输入框的模型选择。显示值按回落链现算、不写回存储；只有用户换模型时才写偏好
 * （同时成为工作区默认）。发送链路按同一条链取值（`use-runtime-transport.ts`）。
 */
export function ModelSelector({ scope, locked }: ModelSelectorProps) {
  const { catalog, status, retry } = useModelCatalog();
  const facts = useSessionLaunchFacts();
  const sessions = useModelSelectionStore((state) => state.sessions);
  const workspaces = useModelSelectionStore((state) => state.workspaces);
  // 回落链只是几次查找，每次渲染现算，不缓存。
  const preferred = catalog ? resolveModelSelection(catalog.models, { sessions, workspaces }, scope, facts) : null;
  const selection = locked === null ? preferred : lockedSelection(locked);
  const selectedModel = catalog?.models.find((model) => model.modelConfigId === selection?.modelConfigId);

  return (
    <ModelAvailability status={status} modelCount={catalog?.models.length ?? 0} onRetry={retry}>
      {locked !== null && !selectedModel && (
        <span className="text-muted-foreground min-w-0 truncate text-xs" title={DELETED_LOCKED_MODEL_HINT}>{DELETED_LOCKED_MODEL_HINT}</span>
      )}
      {selection && catalog && (
        <SelectorView
          catalogModels={catalog.models}
          selection={selection}
          disabled={locked !== null}
          onSelectionChange={(next) => useModelSelectionStore.getState().choose(scope, next)}
        />
      )}
    </ModelAvailability>
  );
}
