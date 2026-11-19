import { WorkspacePathLink } from "@/components/workspace-references/workspace-path-link";
import type { CallView } from "../call-view";
import { readFileChanges, type FileChange } from "@/lib/assistant/tools/file-changes";
import { DiffStat, FileDiff } from "../file-diff";
import { OpenWorkspaceFileButton } from "../open-workspace-file-button";

/** 新建的灰字标签；修改是常态，不标。删除与移动走终端，不进文件变更卡片。 */
export function fileChangeTag(status: string | undefined): string | null {
  return status === "added" ? "新建" : null;
}

function FileChangeSection({ change, single }: { change: FileChange; single: boolean; }) {
  const tag = fileChangeTag(change.status);
  return (
    <section>
      <header className="flex items-center justify-between gap-2 py-1.5 text-xs">
        <span className="flex min-w-0 items-center gap-2">
          <WorkspacePathLink path={change.path} className="font-mono" />
          {tag && <span className="text-muted-foreground shrink-0 text-2xs">{tag}</span>}
        </span>
        {!single && <DiffStat insertions={change.insertions} deletions={change.deletions} />}
      </header>
      <FileDiff change={change} />
    </section>
  );
}

/**
 * `file-changes`（`write_file` / `edit_file` / `apply_patch`）：元信息是 `+a −b`（单个新建文件前加「新建」，
 * 修改是常态不标），没有增删可数时改用文件数或动作标签；单个文件时行尾给「打开」。展开体逐文件给 diff。
 * 回合落定后同一批改动还会按文件聚合进产出区的改动卡，这里是过程视角。补丁部分写入后失败时，错误观察也带着
 * 已写入的这批改动（契约第 4 节），同样按这里显示。
 */
export const fileChangesView: CallView = (data) => {
  const changes = readFileChanges(data.changes);
  // 总数只由逐文件计数相加（后端不另下发汇总）。
  const insertions = changes.reduce((sum, item) => sum + item.insertions, 0);
  const deletions = changes.reduce((sum, item) => sum + item.deletions, 0);
  const tag = changes.length === 1 ? fileChangeTag(changes[0].status) : null;
  const meta = insertions <= 0 && deletions <= 0
    ? changes.length > 1 ? `${changes.length} 个文件` : tag
    : <>{tag}<DiffStat insertions={insertions} deletions={deletions} /></>;
  return {
    actions: changes.length === 1 ? <OpenWorkspaceFileButton path={changes[0].path} /> : undefined,
    meta,
    detail: changes.length > 0 ? {
      render: () => (
        <div className="space-y-1 py-1">
          {changes.map((change) => <FileChangeSection key={change.index} change={change} single={changes.length === 1} />)}
        </div>
      ),
    } : undefined,
  };
};
