import type { SkillImportConflict, SkillImportResolution } from "@/lib/api/extensions";
import type { ConfirmationRequest } from "@/lib/confirm/confirm-store";
import { HttpError } from "@/lib/http/http-error";
import { scopedRootLabel } from "@/lib/skills/skill-directory";

/** 从导入请求的错误里取出冲突明细；不是导入冲突（其他 409、400 等）返回 null，照常当失败处理。 */
export function readSkillImportConflict(error: unknown): SkillImportConflict | null {
  if (!(error instanceof HttpError) || error.status !== 409 || error.code !== "skill_import_conflict") return null;
  const conflict = error.detail?.conflict;
  return typeof conflict === "object" && conflict !== null ? (conflict as SkillImportConflict) : null;
}

/**
 * 按冲突关系生成弹窗（呈现规则只在这里，调用方只补确认后的操作）：判定全部来自后端，这里只把事实说成
 * 用户听得懂的话。`resolution` 是确认后带着再导入一次的选择，为 null 时只告知。
 *
 * - identical：生效的那份已经是这个内容，导入对 Agent 没有变化，只告知在哪；
 * - replace：专属目录里那份会被删掉换成新的，标为不可撤销；
 * - override：本页改用导入的版本，别处那份原样保留，读共享目录的 Codex 等照旧用它。
 */
export function skillImportConflictDialog(
  conflict: SkillImportConflict,
): Omit<ConfirmationRequest, "action"> & { resolution: SkillImportResolution | null } {
  const [top] = conflict.existing;
  const where = (item: SkillImportConflict["existing"][number]) => `${scopedRootLabel(item.scope, item.kind)}  ${item.location}`;
  if (conflict.relation === "identical") {
    return {
      title: `已经有「${conflict.name}」了`,
      description: `内容和要导入的完全相同，不用再导入。\n${where(conflict.existing.find((item) => item.same_content) ?? top)}`,
      confirmLabel: "知道了",
      notice: true,
      resolution: null,
    };
  }
  const versions = `现有：${top.description}\n导入：${conflict.description}`;
  if (conflict.relation === "replace") {
    const replaced = conflict.existing.find((item) => item.kind === "cosir") ?? top;
    return {
      title: `替换「${conflict.name}」？`,
      description: `cosir 专属目录里已有同名 Skill，内容不同。替换会删掉旧的文件，换成导入的这一份。\n${where(replaced)}\n\n${versions}`,
      confirmLabel: "替换",
      irreversible: true,
      tone: "destructive",
      resolution: "replace",
    };
  }
  return {
    title: `仍然导入「${conflict.name}」？`,
    description: `别处已有同名 Skill，内容不同。导入后 cosir 在这里改用导入的版本；那一份原样保留，Codex 等照旧用它。\n${where(top)}\n\n${versions}`,
    confirmLabel: "仍然导入",
    resolution: "override",
  };
}
