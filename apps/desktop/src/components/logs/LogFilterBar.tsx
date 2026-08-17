/**
 * 日志筛选工具栏。
 *
 * 承载 trace_id 输入、关键词输入、时间快捷预设与查询/刷新按钮。
 * 筛选条件由父组件持有，本组件只负责渲染输入与回传变更，不直接发起请求；
 * 回车或点击按钮触发显式查询，时间预设点击即生效（由父组件刷新）。
 *
 * @module components/logs/LogFilterBar
 */

import { RefreshCw, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** 时间快捷筛选预设（分钟数）。 */
const TIME_PRESETS: { label: string; minutes: number }[] = [
  { label: "5m", minutes: 5 },
  { label: "15m", minutes: 15 },
  { label: "1h", minutes: 60 },
];

/** 日志筛选工具栏组件属性。 */
interface LogFilterBarProps {
  /** 当前 trace_id 输入值。 */
  traceId: string;
  /** 当前关键词输入值。 */
  keyword: string;
  /** 是否正在加载（用于禁用查询/刷新与时间预设按钮）。 */
  isLoading: boolean;
  /** trace_id 输入变化回调。 */
  onTraceIdChange: (value: string) => void;
  /** 关键词输入变化回调。 */
  onKeywordChange: (value: string) => void;
  /** 时间快捷预设点击回调（传入分钟数）。 */
  onTimePreset: (minutes: number) => void;
  /** 显式查询/刷新回调（回车或点击按钮触发）。 */
  onSearch: () => void;
}

/**
 * 日志筛选工具栏组件。
 *
 * trace_id / 关键词输入支持回车触发查询；时间预设点击即生效；
 * 右下按钮依据 trace_id 是否为空切换「查询」与「刷新」文案及图标。
 *
 * @param props - 组件属性。
 * @returns 筛选工具栏。
 */
export function LogFilterBar({
  traceId,
  keyword,
  isLoading,
  onTraceIdChange,
  onKeywordChange,
  onTimePreset,
  onSearch,
}: LogFilterBarProps) {
  const isTraceMode = traceId.trim() !== "";

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <Input
        value={traceId}
        onChange={(event) => onTraceIdChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            onSearch();
          }
        }}
        placeholder="trace_id"
        className="h-8 min-w-0 w-40 flex-1 font-mono text-xs"
        aria-label="trace_id"
      />
      <Input
        value={keyword}
        onChange={(event) => onKeywordChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            onSearch();
          }
        }}
        placeholder="关键词过滤"
        className="h-8 min-w-0 w-36 flex-1 text-xs"
        aria-label="关键词过滤"
      />
      {TIME_PRESETS.map((preset) => (
        <Button
          key={preset.label}
          variant="outline"
          size="sm"
          className="h-8 px-2"
          onClick={() => onTimePreset(preset.minutes)}
          disabled={isLoading}
        >
          {preset.label}
        </Button>
      ))}
      <Button variant="outline" size="sm" className="gap-1" onClick={onSearch} disabled={isLoading}>
        {isTraceMode ? (
          <Search className="h-3.5 w-3.5" />
        ) : (
          <RefreshCw className={isLoading ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
        )}
        {isTraceMode ? "查询" : "刷新"}
      </Button>
    </div>
  );
}
