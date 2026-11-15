import { useState } from "react";
import { EyeIcon, EyeOffIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import type { ModelConnectionFields } from "@/lib/api/model-connections";

/** 连接的名称、地址与 Key；新建连接与编辑连接共用。Key 默认密码态，可切换显示。 */
export function ConnectionFields({
  value,
  onChange,
  disabled = false,
}: {
  value: ModelConnectionFields;
  onChange: (next: ModelConnectionFields) => void;
  disabled?: boolean;
}) {
  const [showKey, setShowKey] = useState(false);
  return (
    <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_minmax(0,1.6fr)]">
      <label className="grid min-w-0 gap-1 text-xs">
        连接名称
        <Input value={value.name} disabled={disabled} placeholder="例如：DeepSeek 官方" onChange={(event) => onChange({ ...value, name: event.target.value })} />
      </label>
      <label className="grid min-w-0 gap-1 text-xs">
        API Base URL
        <Input value={value.base_url} disabled={disabled} placeholder="https://api.example.com/v1" onChange={(event) => onChange({ ...value, base_url: event.target.value })} />
      </label>
      <label className="grid min-w-0 gap-1 text-xs">
        API Key
        <div className="relative min-w-0">
          <Input
            type={showKey ? "text" : "password"}
            className="pr-9"
            value={value.api_key}
            disabled={disabled}
            placeholder="请输入 API Key"
            onChange={(event) => onChange({ ...value, api_key: event.target.value })}
          />
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground absolute inset-y-0 right-0 inline-flex w-8 items-center justify-center"
            onClick={() => setShowKey((visible) => !visible)}
            aria-label={showKey ? "隐藏 API Key" : "显示 API Key"}
          >
            {showKey ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
          </button>
        </div>
      </label>
    </div>
  );
}

export function connectionFieldsComplete(value: ModelConnectionFields): boolean {
  return Boolean(value.name.trim() && value.base_url.trim() && value.api_key.trim());
}
