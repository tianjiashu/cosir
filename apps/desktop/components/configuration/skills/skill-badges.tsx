import { Badge } from "@/components/ui/badge";
import type { Skill } from "@/lib/api/extensions";

/**
 * 一个 Skill 的特征徽标（列表行与详情页页头共用）：覆盖同名、仅手动、含脚本。
 * 启用状态由开关表达、所在层由分组或详情页的位置行表达，不再重复做成徽标；
 * 不合标准的警告也不做成徽标：原因要写出来，见 `SkillWarnings`。
 */
export function SkillBadges({ skill }: { skill: Skill }) {
  return (
    <>
      {skill.overrides && <Badge size="sm" tone="neutral" title="优先级更低的目录里有内容不同的同名 Skill，生效的是这一份">覆盖同名</Badge>}
      {skill.manual_only && <Badge size="sm" tone="neutral" title="作者声明只供手动选用，不列入模型看到的 Skill 目录">仅手动</Badge>}
      {skill.has_scripts && <Badge size="sm" tone="warning" title="带 scripts/ 目录：Agent 可能按说明运行其中的脚本">含脚本</Badge>}
    </>
  );
}

/** 不合 Agent Skills 标准的原因；没有警告时不渲染。 */
export function SkillWarnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return <p className="text-warning text-xs">不合 Agent Skills 标准，但可以使用：{warnings.join("；")}</p>;
}
