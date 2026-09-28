"use client";

import { Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { EnvironmentField, EnvironmentGroup } from "@/lib/api/configuration";

export type EnvironmentFieldValue = string | boolean | undefined;

type EnvironmentFieldControlProps = {
  field: EnvironmentField;
  value: EnvironmentFieldValue;
  onChange: (value: string) => void;
  onToggle: () => void;
  onClear: () => void;
};

/**
 * 根据后端字段元数据渲染单个环境配置控件。
 *
 * 组件只负责展示和派发用户操作，不负责保存、脱敏或解释环境变量业务语义；这些行为由
 * 配置页面和后端 service 负责。未知控件类型不会静默降级为可写输入框。
 */
export function EnvironmentFieldControl({
  field,
  value,
  onChange,
  onToggle,
  onClear,
}: EnvironmentFieldControlProps) {
  const inputId = `environment-${field.name}`;
  const enabled = value === true || value === "true";

  return (
    <div className="border-border/70 bg-card/70 rounded-xl border px-3 py-3 shadow-sm">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
        <div className="min-w-0 shrink-0 sm:w-64">
          <label htmlFor={inputId} className="block text-sm font-medium">
            {field.label}
          </label>
          <p className="text-muted-foreground mt-0.5 font-mono text-[10px]">{field.name}</p>
        </div>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {field.component === "checkbox" ? (
            <label className="border-input flex h-9 w-full cursor-pointer items-center gap-2 rounded-lg border px-3 text-sm">
              <input
                id={inputId}
                type="checkbox"
                checked={enabled}
                onChange={onToggle}
                aria-label={field.label}
                className="accent-primary size-4"
              />
              <span>{enabled ? "已启用" : "未启用"}</span>
            </label>
          ) : field.component === "select" ? (
            <select
              id={inputId}
              value={String(value ?? "")}
              onChange={(event) => onChange(event.target.value)}
              aria-label={field.label}
              className="border-input bg-background text-foreground focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full min-w-0 rounded-lg border px-2.5 text-sm outline-none focus-visible:ring-3"
            >
              {field.options.map((option) => (
                <option key={`${field.name}-${option.value}`} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : (
            <Input
              id={inputId}
              className="h-9"
              type={field.component === "password" ? "password" : "text"}
              value={String(value ?? "")}
              placeholder={field.secret && field.masked ? "已配置（留空保持不变）" : field.placeholder ?? "未配置"}
              onChange={(event) => onChange(event.target.value)}
              aria-label={field.label}
            />
          )}
          {field.secret && field.masked && field.clearable && (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="text-destructive shrink-0"
              onClick={onClear}
              aria-label={`清除 ${field.label}`}
              title="清除已保存值"
            >
              <Trash2Icon />
            </Button>
          )}
        </div>
      </div>
      <p className="text-muted-foreground mt-2 text-xs sm:ml-68">{field.description}</p>
    </div>
  );
}

type EnvironmentConfigurationFormProps = {
  groups: EnvironmentGroup[];
  values: Record<string, EnvironmentFieldValue>;
  onChange: (field: EnvironmentField, value: string) => void;
  onToggle: (field: EnvironmentField) => void;
  onClear: (field: EnvironmentField) => void;
};

/**
 * 渲染由后端控制分组和字段元数据的环境配置表单。
 *
 * 表单不直接访问 HTTP，也不生成 changes 请求；调用方可以复用它实现不同的保存策略或预览
 * 模式，同时保持字段组件和分组布局一致。
 */
export function EnvironmentConfigurationForm({
  groups,
  values,
  onChange,
  onToggle,
  onClear,
}: EnvironmentConfigurationFormProps) {
  return (
    <div className="space-y-6">
      {groups.map((group) => (
        <section key={group.id} aria-labelledby={`environment-group-${group.id}`} className="space-y-3">
          <div>
            <h3 id={`environment-group-${group.id}`} className="font-medium">
              {group.label}
            </h3>
            <p className="text-muted-foreground mt-1 text-xs">{group.description}</p>
          </div>
          <div className="space-y-2">
            {group.fields.map((field) => (
              <EnvironmentFieldControl
                key={field.name}
                field={field}
                value={values[field.name]}
                onChange={(value) => onChange(field, value)}
                onToggle={() => onToggle(field)}
                onClear={() => onClear(field)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
