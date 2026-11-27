import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 整份替换的配置写入（文本文档、子 Agent）在基准版本过期时的处理：后端 409
 * `configuration_revision_conflict`，`detail.current_revision` 带当前版本。
 *
 * 版本是读取时拿到的 `revision`（文件内容哈希），由各编辑器在草稿开始时记下、保存时作为
 * `base_revision` 带回；冲突时让用户选：用自己的版本覆盖（以当前版本为基准再存一次），或载入外部版本
 * （放弃草稿）；取消则保持草稿与这次失败，由调用方照常显示错误。按字段或按服务写入的配置没有这一步。
 */

const CONFLICT_CODE = "configuration_revision_conflict";

/** 选了覆盖、但再存一次又冲突（对方还在写）时继续问；用户总能取消，这里只防异常时的死循环。 */
const MAX_ATTEMPTS = 5;

type RevisionConflictChoice = "overwrite" | "reload";

/** 冲突时后端读到的当前版本；不是版本冲突返回 undefined（文件此刻不存在时为 null）。 */
function conflictRevision(error: unknown): string | null | undefined {
  if (!(error instanceof HttpError) || error.status !== 409 || error.code !== CONFLICT_CODE) return undefined;
  const current = error.detail?.current_revision;
  return typeof current === "string" ? current : null;
}

async function askRevisionConflict(label: string): Promise<RevisionConflictChoice | null> {
  let choice: RevisionConflictChoice | null = null;
  await requestConfirmation({
    title: `${label}已在别处修改`,
    description: "开始编辑之后，这份配置被手动编辑、git 或 Agent 改过。覆盖会用你的版本替换那些改动；载入会放弃你还没保存的修改。",
    confirmLabel: "用我的版本覆盖",
    tone: "warning",
    icon: "discard",
    action: async () => { choice = "overwrite"; },
    alternative: { label: "载入外部版本", action: async () => { choice = "reload"; } },
  });
  void frontendLog("WARNING", "configuration_revision_conflict", "配置已在别处修改，等用户选择", {
    data: { label, choice },
  });
  return choice;
}

/**
 * 以 `baseRevision` 为前置条件保存，冲突时问用户：
 * 覆盖就以后端读到的当前版本为基准重存并返回结果；载入返回 `"reload"`，由调用方丢弃草稿、重读；
 * 取消或其他失败原样抛出。
 */
export async function saveResolvingConflicts<T>(
  label: string,
  baseRevision: string | null,
  save: (baseRevision: string | null) => Promise<T>,
): Promise<{ saved: T } | "reload"> {
  let base = baseRevision;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return { saved: await save(base) };
    } catch (error) {
      const current = conflictRevision(error);
      if (current === undefined || attempt >= MAX_ATTEMPTS) throw error;
      const choice = await askRevisionConflict(label);
      if (choice === "reload") return "reload";
      if (choice === null) throw error;
      base = current;
    }
  }
}
