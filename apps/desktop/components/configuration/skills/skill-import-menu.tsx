import { useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDownIcon, Loader2Icon, PlusIcon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { importSkill, type Skill, type SkillImportResolution } from "@/lib/api/extensions";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { desktopHost } from "@/lib/desktop-host";
import { pickLocalFiles } from "@/lib/desktop-host/pick-local-files";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { invalidateSkills } from "@/lib/query/extension-invalidation";
import { readSkillImportConflict, skillImportConflictDialog } from "@/lib/skills/skill-import-conflict";
import { cn } from "@/lib/utils";

type ImportSource = "directory" | "archive";

/**
 * 「导入」菜单：把本机 Skill 文件夹或 ZIP 复制进本作用域的 cosir 专属目录。
 *
 * 专属目录只有 cosir 读，菜单与成功提示都写明这一点，免得用户以为导入会影响 Codex 等工具。
 * `location` 取自后端给本作用域专属分组的位置（各平台不同，前端不写死）；目录还没读到时不显示说明。
 *
 * 冲突由后端判定：第一次导入返回 409 `skill_import_conflict` 时弹窗说明关系，用户确认后在弹窗里带上选择
 * 再导入一次（失败留在弹窗里显示）；「已经有了」只告知。
 */
export function SkillImportMenu({ workspaceId, location }: { workspaceId: number | null; location?: string }) {
  const queryClient = useQueryClient();
  // 原生文件选择框打开期间按钮尚未禁用，用同步标记挡住连击造成的重复弹框。
  const picking = useRef(false);

  function imported(skill: Skill, resolution?: SkillImportResolution) {
    void invalidateSkills(queryClient, workspaceId);
    const copied = location ? `已复制到 ${location}，只有 cosir 使用。` : "只有 cosir 使用。";
    notify.success(`${resolution === "replace" ? "已替换" : "已导入"} ${skill.name}`, {
      description: resolution === "override" ? `${copied}本页改用导入的这一份；别处那份不变，Codex 等照旧用自己的。` : copied,
    });
  }

  const importMutation = useMutation({
    mutationFn: ({ sourcePath, sourceType }: { sourcePath: string; sourceType: ImportSource }) =>
      importSkill(workspaceId, sourcePath, sourceType),
    onSuccess: (skill) => imported(skill),
    onError: (error, { sourcePath, sourceType }) => {
      const conflict = readSkillImportConflict(error);
      if (!conflict) {
        notify.error("导入 Skill 失败", { error, fallback: "导入 Skill 失败，请检查所选目录或 ZIP" });
        return;
      }
      const { resolution, ...dialog } = skillImportConflictDialog(conflict);
      void requestConfirmation({
        ...dialog,
        action: resolution === null
          ? undefined
          : async () => imported(await importSkill(workspaceId, sourcePath, sourceType, resolution), resolution),
        failureFallback: "导入 Skill 失败，请检查所选目录或 ZIP",
      });
    },
  });

  async function selectAndImport(sourceType: ImportSource) {
    if (picking.current || importMutation.isPending) return;
    picking.current = true;
    let sourcePath: string | null;
    try {
      if (!desktopHost) throw new UserFacingError("只能在桌面应用中导入 Skill");
      sourcePath = sourceType === "directory"
        ? await desktopHost.dialog.openDirectory({ title: "选择要导入的 Skill 文件夹" })
        : (await pickLocalFiles({ accept: ".zip" }))[0]?.path || null;
    } catch (error) {
      void frontendLog("ERROR", "skill_import_picker_failed", "打开 Skill 导入选择框失败", { data: { sourceType }, error });
      notify.error("无法打开文件选择框", { error, fallback: "请重试或重启应用" });
      return;
    } finally {
      picking.current = false;
    }
    if (!sourcePath) return;
    importMutation.mutate({ sourcePath, sourceType });
  }

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button variant="outline" size="sm" disabled={importMutation.isPending}>
          {importMutation.isPending ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
          导入
          <ChevronDownIcon />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" className={cn(MENU_CONTENT_CLASS, "min-w-44 text-popover-foreground")}>
          {location && (
            <>
              <DropdownMenu.Label className="text-muted-foreground max-w-64 px-2 py-1.5 text-xs">导入到 {location} · 只有 cosir 使用，Codex 等看不到</DropdownMenu.Label>
              <DropdownMenu.Separator className="my-1 h-px bg-border" />
            </>
          )}
          <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={() => void selectAndImport("directory")}>Skill 文件夹…</DropdownMenu.Item>
          <DropdownMenu.Item className={MENU_ITEM_CLASS} onSelect={() => void selectAndImport("archive")}>ZIP 压缩包…</DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
