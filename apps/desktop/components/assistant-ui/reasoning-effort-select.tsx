"use client";

import { CheckIcon, ChevronDownIcon } from "lucide-react";
import { Select } from "@base-ui/react/select";

import type { ModelSelection } from "@/lib/model-selection-storage";
import { cn } from "@/lib/utils";

type ReasoningEffort = Exclude<ModelSelection["reasoningEffort"], null>;

const EFFORT_OPTIONS: ReadonlyArray<{
  value: ReasoningEffort;
  label: string;
  description: string;
}> = [
  { value: "low", label: "快速", description: "响应更快，适合简单任务" },
  { value: "high", label: "标准", description: "速度和效果平衡" },
  { value: "max", label: "深度", description: "更强推理，可能耗时更长" },
];

function isReasoningEffort(value: unknown): value is ReasoningEffort {
  return value === "low" || value === "high" || value === "max";
}

export function ReasoningEffortSelect({
  value,
  onValueChange,
  className,
}: {
  value: ReasoningEffort;
  onValueChange: (value: ReasoningEffort) => void;
  className?: string;
}) {
  return (
    <div className={cn("inline-flex", className)}>
      <Select.Root
        items={EFFORT_OPTIONS.map(({ value: optionValue, label }) => ({ value: optionValue, label }))}
        value={value}
        onValueChange={(nextValue) => {
          if (isReasoningEffort(nextValue)) onValueChange(nextValue);
        }}
      >
        <Select.Label className="sr-only">推理深度</Select.Label>
        <Select.Trigger
          aria-label="推理深度"
          className="text-muted-foreground hover:text-foreground data-pressed:bg-muted/50 inline-flex h-8 min-w-24 items-center gap-1.5 rounded-md px-2 text-xs whitespace-nowrap outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/50"
        >
          <span>推理</span>
          <span aria-hidden="true">·</span>
          <Select.Value className="text-foreground font-medium" />
          <Select.Icon>
            <ChevronDownIcon className="size-3" />
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner side="top" align="start" sideOffset={6} className="z-50 outline-none">
            <Select.Popup className="bg-popover text-popover-foreground min-w-60 rounded-xl border p-1.5 shadow-lg outline-none data-starting-style:scale-95 data-starting-style:opacity-0 data-ending-style:scale-95 data-ending-style:opacity-0 transition-[opacity,transform]">
              <div className="text-muted-foreground border-b px-2 py-1.5 text-xs font-medium">推理深度</div>
              <Select.List className="max-h-72 overflow-y-auto py-1">
                {EFFORT_OPTIONS.map((option) => (
                  <Select.Item
                    key={option.value}
                    value={option.value}
                    className="hover:bg-muted data-highlighted:bg-muted grid min-h-10 grid-cols-[1rem_minmax(0,1fr)] items-center gap-2 rounded-md px-2 py-1.5 text-left outline-none"
                  >
                    <Select.ItemIndicator className="col-start-1 row-span-2">
                      <CheckIcon className="text-primary size-4" />
                    </Select.ItemIndicator>
                    <div className="col-start-2 min-w-0">
                      <Select.ItemText className="block text-sm leading-5">{option.label}</Select.ItemText>
                      <span className="text-muted-foreground block text-xs leading-4">{option.description}</span>
                    </div>
                  </Select.Item>
                ))}
              </Select.List>
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
