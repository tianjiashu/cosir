import { useMemo } from "react";

import { useWebOnlyLinkOpener, type LinkOpener } from "@/components/links/use-open-link";
import type { SkillDetail } from "@/lib/api/extensions";
import { inlineCodeLink, resolveLink, type LinkTarget } from "@/lib/links/resolve-link";
import { baseName } from "@/lib/files/file-name";

/**
 * Skill 详情页的打开方式：Skill 说明里的相对路径指向它自己的配套文件（`scripts/`、`references/`），
 * 点了在右侧阅读区打开、在文件树里展开到它；网页与页内锚点照常交给只认网页的打开器（外部地址先确认）。
 *
 * 根是 Skill 目录，解析规则沿用 `resolveLink`（越出目录的路径不可点）。只认文件清单里存在、能预览的文件：
 * 写错的路径与二进制文件渲染为纯文本，不给点了打不开的入口。行内代码里只有文件名时，Skill 里恰好一个
 * 同名文件才认。
 */
export function useSkillLinkOpener(skill: SkillDetail, openFile: (path: string) => void): LinkOpener {
  const web = useWebOnlyLinkOpener("configuration");
  return useMemo(() => {
    const readable = skill.files.filter((file) => !file.binary).map((file) => file.path);
    const base = { workspaceRoot: skill.directory };
    const inSkill = (target: LinkTarget | null): LinkTarget | null => {
      if (target?.kind === "workspace") return !target.directory && readable.includes(target.path) ? target : null;
      if (target?.kind === "filename-search") {
        const matches = readable.filter((path) => baseName(path) === target.query);
        return matches.length === 1 ? { kind: "workspace", path: matches[0], directory: false } : null;
      }
      return target?.kind === "page" ? null : target;
    };
    return {
      workspaceRoot: skill.directory,
      resolve: (href, documentDir) => inSkill(resolveLink(href, { ...base, documentDir })),
      resolveCode: (text, documentDir) => inSkill(inlineCodeLink(text, { ...base, documentDir })),
      resolveDeliverable: web.resolveDeliverable,
      describe: (target) => (target.kind === "workspace" ? `打开 ${target.path}` : web.describe(target)),
      open: (target, origin) => {
        if (target.kind === "workspace") openFile(target.path);
        else web.open(target, origin);
      },
    };
  }, [skill, openFile, web]);
}
