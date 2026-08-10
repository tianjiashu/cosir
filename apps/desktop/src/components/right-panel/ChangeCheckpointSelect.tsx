/**
 * 变更集检查点选择组件。
 *
 * 用原生 ``<select>`` 提供「最新」+ 各检查点的下拉切换（项目 ui 目录未安装
 * shadcn Select，LogsPage 亦用原生 select 风格）。
 *
 * @module components/right-panel/ChangeCheckpointSelect
 */

import type { ChangeCheckpoint } from "@shared/api";

/** ChangeCheckpointSelect 组件属性。 */
interface ChangeCheckpointSelectProps {
  /** 检查点列表（一个 turn 对应一个）。 */
  checkpoints: ChangeCheckpoint[];
  /** 当前选中的检查点 turn 标识；null 表示展示全部（最新）。 */
  value: string | null;
  /** 切换检查点（null 表示展示全部）。 */
  onChange: (turnId: string | null) => void;
}

/**
 * 变更集检查点下拉选择。
 *
 * @param props - 组件属性。
 * @returns 检查点选择下拉。
 */
export function ChangeCheckpointSelect({
  checkpoints,
  value,
  onChange,
}: ChangeCheckpointSelectProps) {
  return (
    <select
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value === "" ? null : event.target.value)}
      aria-label="变更检查点"
      // 160px 是检查点下拉控件的紧凑固定宽度，避免长标签挤占操作区。
      // eslint-disable-next-line tailwind/no-arbitrary-value
      className="h-8 w-[160px] shrink-0 rounded-md border border-input bg-background px-2 text-xs"
    >
      <option value="">最新</option>
      {checkpoints.map((checkpoint) => (
        <option key={checkpoint.turn_id} value={checkpoint.turn_id}>
          {checkpoint.label}
        </option>
      ))}
    </select>
  );
}
