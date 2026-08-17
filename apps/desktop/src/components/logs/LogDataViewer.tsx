/**
 * 结构化 JSON 数据查看器。
 *
 * 将后端返回的 `data` 或 `error` 字段（Record<string, unknown>）渲染为
 * 带语法高亮的格式化可读内容，不引入任何新依赖，纯 React 递归渲染。
 *
 * @module components/logs/LogDataViewer
 */

import { Fragment } from "react";
import { Caption } from "@/components/ui/tokens";

/** 待渲染的任意 JSON 值类型。 */
type JsonValue = unknown;

/**
 * 值 token 的着色类名映射。
 *
 * 按 JSON 原始类型分配 Tailwind 文本色，统一视觉效果。
 *
 * @param value - 待判断的 JSON 值。
 * @returns 对应的文本色类名。
 */
function valueColorClass(value: JsonValue): string {
  if (typeof value === "string") return "text-emerald-600";
  if (typeof value === "number") return "text-amber-600";
  if (typeof value === "boolean") return "text-sky-600";
  if (value === null) return "text-muted-foreground";
  return "text-foreground";
}

/**
 * 渲染单个 JSON 值的内联表示。
 *
 * 字符串带引号，null 显示小写，其它标量直接 toString，对象/数组交由外层递归。
 *
 * @param value - 待渲染的 JSON 值。
 * @returns 着色后的文本节点。
 */
function renderInlineValue(value: JsonValue): React.ReactNode {
  if (typeof value === "string") {
    return <span className="text-emerald-600">"{value}"</span>;
  }
  if (value === null) {
    return <span className="text-muted-foreground">null</span>;
  }
  return <span className={valueColorClass(value)}>{String(value)}</span>;
}

/**
 * LogDataViewer 组件属性。
 */
interface LogDataViewerProps {
  /** 待渲染的结构化对象（data 或 error）。 */
  data: Record<string, unknown>;
}

/**
 * 结构化 JSON 数据查看器组件。
 *
 * 顶层为对象键值对列表，每个键名高亮、值按类型着色；嵌套对象/数组折叠为
 * 单行预览（{…} / […]，不展开深层结构，避免日志过长），标量值完整呈现。
 *
 * @param props - 组件属性。
 * @returns 可读的格式化 JSON 视图。
 */
export function LogDataViewer({ data }: LogDataViewerProps) {
  const entries = Object.entries(data);

  if (entries.length === 0) {
    return <span className="text-muted-foreground">（空对象）</span>;
  }

  return (
    <div className="space-y-0.5">
      {entries.map(([key, value]) => {
        const isNested = value !== null && typeof value === "object";
        return (
          <div key={key} className="flex flex-wrap gap-1 leading-5">
            <span className="font-medium text-violet-600">{key}:</span>
            {isNested ? (
              <span className="text-muted-foreground">
                {Array.isArray(value) ? "[…]" : "{…}"}
              </span>
            ) : (
              <Fragment>{renderInlineValue(value)}</Fragment>
            )}
          </div>
        );
      })}
      <p className={Caption.xs10 + " text-muted-foreground"}>
        嵌套对象/数组已折叠预览，完整内容见后端日志文件。
      </p>
    </div>
  );
}
