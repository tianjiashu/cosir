import { useState } from "react";
import { CheckCircle2Icon, XIcon, XCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { Spinner } from "@/components/ui/spinner";
import type { ModelCapabilities, ModelConfigFields } from "@/lib/api/model-connections";
import { DEFAULT_CAPABILITIES, ModelCapabilityFields, pickCapabilities } from "./model-capability-fields";
import type { ModelTestState } from "./test-model-drafts";

/** 待保存的一个模型：字段加上它最近一次测试的结果；字段一改，结果作废回到 idle。 */
export type ModelDraft = ModelConfigFields & { test: ModelTestState };

/**
 * 已选模型：每行一个模型，可单独改能力、移除，并显示测试进度与失败原因。
 * 「统一设置」把同一组能力一次应用到全部行，之后仍可逐行改。
 */
export function ModelDraftList({
  drafts,
  onChange,
  onRemove,
  disabled = false,
}: {
  drafts: readonly ModelDraft[];
  onChange: (modelName: string, capabilities: ModelCapabilities) => void;
  onRemove: (modelName: string) => void;
  disabled?: boolean;
}) {
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulk, setBulk] = useState<ModelCapabilities>(DEFAULT_CAPABILITIES);

  if (drafts.length === 0) {
    return (
      <div className="text-muted-foreground border-border/70 flex min-h-24 items-center justify-center rounded-md border border-dashed px-3 text-center text-xs">
        勾选左侧模型，或手动输入模型名称
      </div>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="font-medium">已选 {drafts.length} 个模型</span>
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground focus-ring inline-flex items-center gap-0.5 rounded px-1"
          aria-expanded={bulkOpen}
          onClick={() => setBulkOpen((open) => !open)}
        >
          统一设置
          <DisclosureChevron open={bulkOpen} />
        </button>
      </div>
      {bulkOpen && (
        <div className="bg-muted/50 flex flex-wrap items-center justify-between gap-2 rounded-md px-2.5 py-2">
          <ModelCapabilityFields value={bulk} onChange={setBulk} disabled={disabled} label="统一设置" />
          <Button
            size="xs"
            variant="outline"
            disabled={disabled}
            onClick={() => {
              for (const draft of drafts) onChange(draft.model_name, bulk);
              setBulkOpen(false);
            }}
          >
            应用到全部 {drafts.length} 个
          </Button>
        </div>
      )}
      <ul className="border-border/70 divide-border/70 max-h-96 divide-y overflow-y-auto rounded-md border">
        {drafts.map((draft) => (
          <li key={draft.model_name} className="grid gap-1.5 px-2.5 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm" title={draft.model_name}>{draft.model_name}</span>
              <TestStatus state={draft.test} />
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`移除 ${draft.model_name}`}
                disabled={disabled}
                onClick={() => onRemove(draft.model_name)}
              >
                <XIcon />
              </Button>
            </div>
            <ModelCapabilityFields
              value={pickCapabilities(draft)}
              onChange={(next) => onChange(draft.model_name, next)}
              disabled={disabled}
              label={draft.model_name}
            />
            {draft.test.kind === "failed" && <p className="text-destructive text-xs">{draft.test.message}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function TestStatus({ state }: { state: ModelTestState }) {
  switch (state.kind) {
    case "idle":
      return null;
    case "testing":
      return <span className="text-muted-foreground flex items-center gap-1 text-xs"><Spinner className="size-3.5" label={null} />测试中</span>;
    case "passed":
      return (
        <span className="text-success flex items-center gap-1 text-xs">
          <CheckCircle2Icon className="size-3.5" />通过{state.elapsedMs ? ` · ${state.elapsedMs}ms` : ""}
        </span>
      );
    case "failed":
      return <span className="text-destructive flex items-center gap-1 text-xs"><XCircleIcon className="size-3.5" />未通过</span>;
  }
}
