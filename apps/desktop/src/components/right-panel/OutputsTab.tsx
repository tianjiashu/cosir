/**
 * Outputs 列表组件。
 *
 * 展示任务产生/修改的文件、文档、代码片段等结果。
 * 第一版为静态只读列表。
 *
 * @module components/right-panel/OutputsTab
 */

import { FileText } from "lucide-react";

/** Output 条目数据接口。 */
export interface OutputItem {
  /** 唯一标识。 */
  id: string;
  /** 文件或产物名称。 */
  name: string;
  /** 简短描述。 */
  description: string;
}

/** OutputsTab 组件属性。 */
interface OutputsTabProps {
  /** Output 数据列表（第一版为 mock 数据）。 */
  items: OutputItem[];
}

/**
 * OutputsTab Outputs 列表组件。
 *
 * 渲染任务产出的文件和代码片段列表，
 * 每项显示文件图标、路径名和描述。
 */
export function OutputsTab({ items }: OutputsTabProps) {
  return (
    <div className="space-y-1 p-3">
      {items.map((item) => (
        <button
          key={item.id}
          className="flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent/50 transition-colors"
          title={`${item.name}: ${item.description}`}
        >
          <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-xs">{item.name}</p>
            <p className="truncate text-xs text-muted-foreground">{item.description}</p>
          </div>
        </button>
      ))}

      {items.length === 0 && (
        <p className="py-8 text-center text-xs text-muted-foreground">暂无输出</p>
      )}
    </div>
  );
}
