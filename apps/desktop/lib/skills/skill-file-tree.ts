import type { SkillDetail } from "@/lib/api/extensions";

export type SkillFile = SkillDetail["files"][number];

/** 文件树的一个节点；`fileCount` 是文件夹里（含子文件夹）的文件数，收起时给用户一个规模感。 */
export type SkillTreeNode =
  | { kind: "directory"; path: string; name: string; fileCount: number; children: SkillTreeNode[] }
  | { kind: "file"; path: string; name: string; file: SkillFile };

/** Skill 的入口文件，详情页默认打开它，文件树里排在最前。 */
export const SKILL_ENTRY_FILE = "SKILL.md";

/**
 * 把后端给的扁平文件清单（路径相对 Skill 目录、POSIX 分隔）组成树。
 *
 * 中间层文件夹由路径本身推出，只有子文件夹、没有直接文件的文件夹也会出现。同一层里文件夹在前、文件在后，
 * 各按名称排序；根上的 `SKILL.md` 是入口，排在最前。
 */
export function buildSkillFileTree(files: SkillFile[]): SkillTreeNode[] {
  type Draft = { directories: Map<string, Draft>; files: SkillFile[] };
  const root: Draft = { directories: new Map(), files: [] };
  for (const file of files) {
    let current = root;
    for (const segment of file.path.split("/").slice(0, -1)) {
      let next = current.directories.get(segment);
      if (!next) {
        next = { directories: new Map(), files: [] };
        current.directories.set(segment, next);
      }
      current = next;
    }
    current.files.push(file);
  }

  const byName = (left: { name: string }, right: { name: string }) => left.name.localeCompare(right.name);
  const toNodes = (draft: Draft, prefix: string): SkillTreeNode[] => {
    const directories = [...draft.directories].map(([name, child]) => {
      const path = `${prefix}${name}`;
      const children = toNodes(child, `${path}/`);
      const fileCount = children.reduce((count, node) => count + (node.kind === "file" ? 1 : node.fileCount), 0);
      return { kind: "directory" as const, path, name, fileCount, children };
    }).sort(byName);
    const leaves = draft.files
      .map((file) => ({ kind: "file" as const, path: file.path, name: file.path.slice(prefix.length), file }))
      .sort(byName);
    const entry = leaves.filter((leaf) => leaf.path === SKILL_ENTRY_FILE);
    return [...entry, ...directories, ...leaves.filter((leaf) => leaf.path !== SKILL_ENTRY_FILE)];
  };
  return toNodes(root, "");
}

/** 一个文件的各级上层文件夹路径（`a/b/c.md` → `["a", "a/b"]`），用于选中时展开到它。 */
export function ancestorDirectories(path: string): string[] {
  const segments = path.split("/").slice(0, -1);
  return segments.map((_, index) => segments.slice(0, index + 1).join("/"));
}
