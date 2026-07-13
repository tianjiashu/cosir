/**
 * 项目列表组件。
 *
 * 展示项目列表，支持选中态和标题截断。
 * 第一版使用静态 mock 数据。
 *
 * @module components/sidebar/ProjectList
 */

import { FolderOpen, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/** 项目数据接口。 */
export interface ProjectItem {
  /** 项目唯一标识。 */
  id: string;
  /** 项目显示名称。 */
  name: string;
  /** 项目文件系统路径。 */
  path: string;
}

/** ProjectList 组件属性。 */
interface ProjectListProps {
  /** 项目数据列表（第一版为 mock 数据）。 */
  projects: ProjectItem[];
  /** 当前选中项目 ID。 */
  activeId: string | null;
  /** 选中回调。 */
  onSelect: (id: string) => void;
}

/**
 * ProjectList 项目列表组件。
 *
 * 渲染可点击的项目列表，当前选中项有明确高亮，
 * 长标题自动截断不撑破布局。
 */
export function ProjectList({ projects, activeId, onSelect }: ProjectListProps) {
  return (
    <div className="px-2 py-1">
      {/* 标题 */}
      <div className="mb-1 flex items-center gap-1 px-2 py-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <FolderOpen className="h-3.5 w-3.5" />
        项目
      </div>

      {/* 列表 */}
      {projects.map((project) => (
        <button
          key={project.id}
          onClick={() => onSelect(project.id)}
          title={`${project.name} (${project.path})`}
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
            activeId === project.id
              ? "bg-accent text-accent-foreground"
              : "hover:bg-accent/50 text-muted-foreground",
          )}
        >
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 shrink-0 transition-transform",
              activeId === project.id && "rotate-90",
            )}
          />
          <span className="truncate">{project.name}</span>
        </button>
      ))}
    </div>
  );
}
