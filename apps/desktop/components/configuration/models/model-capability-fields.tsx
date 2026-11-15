import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import type { ModelCapabilities } from "@/lib/api/model-connections";
import { cn } from "@/lib/utils";

/** 新行的默认能力：能力由用户声明，默认什么都不勾。 */
export const DEFAULT_CAPABILITIES: ModelCapabilities = {
  context_window_k: 128,
  supports_thinking: false,
  supports_reasoning_effort: false,
  supports_image: false,
};

const FLAGS = [
  ["supports_thinking", "思考"],
  ["supports_reasoning_effort", "推理强度"],
  ["supports_image", "图片输入"],
] as const;

export function capabilitiesValid(value: ModelCapabilities): boolean {
  return Number.isInteger(value.context_window_k) && value.context_window_k > 0;
}

export function pickCapabilities(value: ModelCapabilities): ModelCapabilities {
  return {
    context_window_k: value.context_window_k,
    supports_thinking: value.supports_thinking,
    supports_reasoning_effort: value.supports_reasoning_effort,
    supports_image: value.supports_image,
  };
}

/**
 * 一个模型的上下文窗口与三项能力，一行排开；已选模型行、模型编辑与「统一设置」共用。
 * 上下文窗口清空时记为 NaN，由 {@link capabilitiesValid} 拦下保存。
 */
export function ModelCapabilityFields({
  value,
  onChange,
  disabled = false,
  label,
}: {
  value: ModelCapabilities;
  onChange: (next: ModelCapabilities) => void;
  disabled?: boolean;
  /** 区分同屏多行的读屏名称，如模型名。 */
  label: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
      <label className="flex items-center gap-1.5">
        上下文
        <Input
          type="number"
          min="1"
          step="1"
          inputMode="numeric"
          aria-label={`${label} 上下文窗口（K）`}
          className={cn("h-7 w-20 px-2 text-xs", !capabilitiesValid(value) && "border-destructive")}
          value={Number.isNaN(value.context_window_k) ? "" : String(value.context_window_k)}
          disabled={disabled}
          onChange={(event) => onChange({ ...value, context_window_k: event.target.value === "" ? Number.NaN : Number(event.target.value) })}
        />
        K
      </label>
      {FLAGS.map(([key, text]) => (
        <label key={key} className="flex items-center gap-1.5">
          <Checkbox
            checked={value[key]}
            disabled={disabled}
            aria-label={`${label} ${text}`}
            onChange={(event) => onChange({ ...value, [key]: event.target.checked })}
          />
          {text}
        </label>
      ))}
    </div>
  );
}

/** 模型行里的只读摘要：「128K · 思考 · 图片输入」。 */
export function capabilitySummary(value: ModelCapabilities): string {
  return [`${value.context_window_k}K`, ...FLAGS.filter(([key]) => value[key]).map(([, text]) => text)].join(" · ");
}
