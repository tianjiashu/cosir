import { useCallback, useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { formatByteSize } from "@/lib/format/byte-size";
import { ancestorDirectories, buildSkillFileTree, SKILL_ENTRY_FILE, type SkillFile, type SkillTreeNode } from "@/lib/skills/skill-file-tree";
import { cn } from "@/lib/utils";

/**
 * 详情页的选中文件与展开状态。文件夹默认收起（先看全貌，各文件夹显示文件数）；选中一个文件——点树、点正文里的
 * 链接都一样——就展开到它，用户自己收起的文件夹不会被重新打开，除非又选中了其中的文件。
 */
export function useSkillFileTreeState() {
  const [selected, setSelected] = useState(SKILL_ENTRY_FILE);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  // 稳定引用：正文链接的打开器依赖 `select`，每次渲染换一个会让链接组件跟着重建。
  const select = useCallback((path: string) => {
    setSelected(path);
    setExpanded((current) => new Set([...current, ...ancestorDirectories(path)]));
  }, []);
  const toggle = useCallback((directory: string) => setExpanded((current) => {
    const next = new Set(current);
    if (!next.delete(directory)) next.add(directory);
    return next;
  }), []);
  return { selected, expanded, select, toggle };
}

/** Skill 随附文件的树；二进制文件只列出、不提供预览。 */
export function SkillFileTree({ files, truncated, selected, expanded, onSelect, onToggle }: {
  files: SkillFile[];
  truncated: boolean;
  selected: string;
  expanded: ReadonlySet<string>;
  onSelect: (path: string) => void;
  onToggle: (directory: string) => void;
}) {
  const renderNodes = (nodes: SkillTreeNode[], depth: number) => nodes.map((node) => {
    const indent = { paddingLeft: `${depth * 12 + 8}px` };
    if (node.kind === "directory") {
      const open = expanded.has(node.path);
      return (
        <li key={node.path}>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => onToggle(node.path)}
            className="focus-ring text-muted-foreground hover:bg-muted/60 hover:text-foreground flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left"
            style={indent}
          >
            <ChevronRightIcon className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")} />
            <WorkspaceFileIcon name={node.name} folder open={open} />
            <span className="min-w-0 flex-1 truncate">{node.name}</span>
            <span className="shrink-0 tabular-nums opacity-70">{node.fileCount}</span>
          </button>
          {open && <ul className="space-y-px">{renderNodes(node.children, depth + 1)}</ul>}
        </li>
      );
    }
    const { file } = node;
    return (
      <li key={node.path}>
        <button
          type="button"
          disabled={file.binary}
          aria-current={node.path === selected ? "true" : undefined}
          title={file.binary ? "二进制文件，不提供预览" : node.path}
          onClick={() => onSelect(node.path)}
          className={cn(
            "focus-ring flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left",
            node.path === selected ? "bg-muted text-foreground font-medium" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            file.binary && "cursor-default opacity-60 hover:bg-transparent",
          )}
          // 文件比同层文件夹多缩进一个箭头宽，名称才与文件夹名对齐。
          style={{ paddingLeft: `${depth * 12 + 8 + 18}px` }}
        >
          <WorkspaceFileIcon name={node.name} />
          <span className="min-w-0 flex-1 truncate">{node.name}</span>
          <span className="shrink-0 tabular-nums opacity-70">{file.binary ? "二进制" : formatByteSize(file.size)}</span>
        </button>
      </li>
    );
  });

  return (
    <nav aria-label="Skill 文件" className="max-h-48 overflow-y-auto border-b p-2 lg:max-h-none lg:border-r lg:border-b-0">
      <ul className="space-y-px text-xs">{renderNodes(buildSkillFileTree(files), 0)}</ul>
      {truncated && (
        <p className="text-muted-foreground px-2 pt-2 text-xs">文件太多，只列出了一部分；完整内容请在文件夹中查看。</p>
      )}
    </nav>
  );
}
