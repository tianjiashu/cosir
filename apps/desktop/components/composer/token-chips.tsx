import { FileTextIcon, HammerIcon } from "lucide-react";

import { InlineToken } from "@/components/ui/inline-token";

type ChipRemove = { label: string; disabled?: boolean; onClick: () => void };

/** Skill 胶囊：输入框与已发送消息共用，消息里不传 `remove`。 */
export function SkillChip({ name, remove }: { name: string; remove?: ChipRemove }) {
  return <InlineToken icon={<HammerIcon />} name={name} description={`Skill：${name}`} tone="skill" remove={remove} />;
}

/**
 * 普通文件胶囊：输入框与已发送消息共用。`state` 只在输入框里有意义（附件登记尚在同步或已失效），
 * 消息里的文件来自后端展示绑定，恒为就绪。
 */
export function FileChip({ name, description, state = "ready", remove }: {
  name: string;
  description: string;
  state?: "ready" | "pending" | "error";
  remove?: ChipRemove;
}) {
  return (
    <InlineToken
      icon={<FileTextIcon />}
      name={name}
      description={description}
      preserveExtension={state === "ready"}
      state={state}
      remove={remove}
    />
  );
}
