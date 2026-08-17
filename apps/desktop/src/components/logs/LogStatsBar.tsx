/**
 * 日志级别计数栏。
 *
 * 展示后端返回的各级别计数（level_counts）并以 Badge 呈现；点击某级别触发快捷筛选，
 * 点击「全部」清除级别筛选。级别数据不再由当页 entries 本地统计，而是由后端全量统计，
 * 保证级别分布真实反映当前过滤集。
 *
 * @module components/logs/LogStatsBar
 */

import type { LogLevel } from "@shared/logs";
import { Badge } from "@/components/ui/badge";

/** 参与统计与快捷筛选的级别。 */
const STAT_LEVELS: LogLevel[] = ["ERROR", "WARNING", "INFO", "DEBUG"];

/** 级别到徽标配色的映射。 */
const BADGE_VARIANT: Record<string, "destructive" | "warning" | "secondary" | "outline"> = {
  ERROR: "destructive",
  WARNING: "warning",
  INFO: "secondary",
  DEBUG: "outline",
};

/**
 * 日志级别计数栏组件属性。
 */
interface LogStatsBarProps {
  /** 各级别计数（后端返回，忽略 level 过滤、含其余过滤条件）。 */
  levelCounts: Record<string, number>;
  /** 当前过滤集下的总条数（用于「全部」入口）。 */
  total: number;
  /** 当前生效的级别筛选（空串表示全部）；用于控制选中态高亮。 */
  activeLevel: LogLevel | "";
  /** 点击某级别或「全部」时的回调，用于快捷筛选。 */
  onSelectLevel: (level: LogLevel | "") => void;
}

/**
 * 日志级别计数栏组件。
 *
 * 渲染一个「全部」入口与各主要级别带计数的 Badge；点击 Badge 调用 onSelectLevel
 * 切换筛选，被选中的级别以高亮边框标识。
 *
 * @param props - 组件属性。
 * @returns 顶部级别计数栏。
 */
export function LogStatsBar({ levelCounts, total, activeLevel, onSelectLevel }: LogStatsBarProps) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-4 py-1.5">
      <button
        type="button"
        onClick={() => onSelectLevel("")}
        className="transition-opacity hover:opacity-80"
        aria-pressed={activeLevel === ""}
      >
        <Badge variant="secondary" className={activeLevel === "" ? "ring-2 ring-ring" : ""}>
          全部 {total}
        </Badge>
      </button>
      {STAT_LEVELS.map((level) => {
        const active = activeLevel === level;
        return (
          <button
            key={level}
            type="button"
            onClick={() => onSelectLevel(level)}
            className="transition-opacity hover:opacity-80"
            aria-pressed={active}
          >
            <Badge variant={BADGE_VARIANT[level]} className={active ? "ring-2 ring-ring" : ""}>
              {level} {levelCounts[level] ?? 0}
            </Badge>
          </button>
        );
      })}
    </div>
  );
}
