import { useMemo } from "react";

import {
  ModelSelectorContent,
  ModelSelectorEffort,
  ModelSelectorList,
  ModelSelectorRoot,
  ModelSelectorSearch,
  ModelSelectorTrigger,
  ModelSelectorValue,
  DEFAULT_EFFORT_OPTIONS,
  type ModelOption,
} from "@/components/model/model-selector";
import { ModelAvailability } from "@/components/model/model-availability";
import { COMPOSER_CONFIG_BUTTON_CLASS_NAME } from "@/components/composer/composer-surface";
import { cn } from "@/lib/utils";
import type { ComposerLaunch } from "@/lib/assistant/composer/composer-launch";
import { RUN_LAUNCH_LOCKED_HINT } from "@/lib/assistant/run-launch";
import { catalogModelOption, useModelCatalog, type ModelCatalogModel } from "@/lib/model/catalog";
import {
  selectionFor,
  useModelSelectionStore,
  type ModelSelection,
  type ModelSelectionScope,
} from "@/lib/model/model-selection-store";

type ModelSelectorProps = {
  scope: ModelSelectionScope;
  /** 输入框的发起配置结论（`useComposerLaunch`）；锁定时显示 Run 冻结的配置且不可更改，也不写回偏好。 */
  launch: ComposerLaunch;
};

function toModelOption(model: ModelCatalogModel): ModelOption {
  return { ...catalogModelOption(model), efforts: model.supportsReasoningEffort ? DEFAULT_EFFORT_OPTIONS : undefined };
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
    () => catalogModels.map(toModelOption),
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
      <ModelSelectorRoot
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
        <ModelSelectorTrigger
          variant="ghost"
          indicator={false}
          className={cn(COMPOSER_CONFIG_BUTTON_CLASS_NAME, "min-w-0 shrink max-w-full @lg:max-w-60")}
          aria-label="选择模型"
          disabled={disabled}
        >
          <ModelSelectorValue className="min-w-0" />
        </ModelSelectorTrigger>
        <ModelSelectorContent
          side="top"
          align="end"
          searchable
          className="w-72"
        >
          <ModelSelectorSearch placeholder="搜索模型…" />
          <ModelSelectorList />
          <ModelSelectorEffort label="推理强度" />
        </ModelSelectorContent>
      </ModelSelectorRoot>
    </div>
  );
}

const DELETED_LOCKED_MODEL_HINT = "运行中的模型配置已删除，停止后重新选择";

/**
 * 会话输入框的模型选择：只显示 {@link ComposerLaunch} 这份结论（锁定时只读、未锁定时可换），不再自己推导；
 * 只有用户换模型时才写偏好（同时成为工作区默认）。目录加载中、失败或为空时由 `ModelAvailability` 说明。
 */
export function ModelSelector({ scope, launch }: ModelSelectorProps) {
  const { catalog, status, retry } = useModelCatalog();
  const { selection, locked } = launch;
  const selectedModel = catalog?.models.find((model) => model.modelConfigId === selection?.modelConfigId);

  return (
    <ModelAvailability status={status} modelCount={catalog?.models.length ?? 0} onRetry={retry}>
      {locked && !selectedModel && (
        <span className="text-muted-foreground min-w-0 truncate text-xs" title={DELETED_LOCKED_MODEL_HINT}>{DELETED_LOCKED_MODEL_HINT}</span>
      )}
      {selection && catalog && (
        <SelectorView
          catalogModels={catalog.models}
          selection={selection}
          disabled={locked}
          onSelectionChange={(next) => useModelSelectionStore.getState().choose(scope, next)}
        />
      )}
    </ModelAvailability>
  );
}
