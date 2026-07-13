/**
 * Sources 列表组件。
 *
 * 展示本轮任务引用的文档、上下文片段或规则文件。
 * 第一版为静态只读列表。
 *
 * @module components/right-panel/SourcesTab
 */

import { Link2 } from "lucide-react";

/** Source 条目数据接口。 */
export interface SourceItem {
  /** 唯一标识。 */
  id: string;
  /** 来源名称（通常是文件路径）。 */
  name: string;
  /** 简短描述。 */
  description: string;
}

/** SourcesTab 组件属性。 */
interface SourcesTabProps {
  /** Source 数据列表（第一版为 mock 数据）。 */
  items: SourceItem[];
}

/**
 * SourcesTab Sources 列表组件。
 *
 * 渲染本轮任务引用的文档和规则文件列表，
 * 每项显示链接图标、路径名和描述。
 */
export function SourcesTab({ items }: SourcesTabProps) {
  return (
    <div className="space-y-1 p-3">
      {items.map((item) => (
        <button
          key={item.id}
          className="flex w-full items-start gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent/50 transition-colors"
          title={`${item.name}: ${item.description}`}
        >
          <Link2 className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-xs">{item.name}</p>
            <p className="truncate text-xs text-muted-foreground">{item.description}</p>
          </div>
        </button>
      ))}

      {items.length === 0 && (
        <p className="py-8 text-center text-xs text-muted-foreground">暂无引用来源</p>
      )}
    </div>
  );
}
