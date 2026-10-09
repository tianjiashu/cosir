import type { ReactNode } from "react";
import { BotIcon, UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ConfigurationProposalMode } from "@/lib/assistant/configuration-proposal-mode";

export type { ConfigurationProposalMode } from "@/lib/assistant/configuration-proposal-mode";

/** 在发送本轮消息时选择临时开放的配置草稿生成工具。 */
export function ConfigurationProposalModeSelector({
  value,
  onChange,
  disabled = false,
}: {
  value: ConfigurationProposalMode;
  onChange: (value: ConfigurationProposalMode) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <ProposalModeButton
        active={value === "agent"}
        disabled={disabled}
        onClick={() => onChange(value === "agent" ? null : "agent")}
        icon={<BotIcon className="size-3.5" />}
        label="生成子 Agent 配置"
      />
      <ProposalModeButton
        active={value === "agent-team"}
        disabled={disabled}
        onClick={() => onChange(value === "agent-team" ? null : "agent-team")}
        icon={<UsersIcon className="size-3.5" />}
        label="生成 Agent Team 配置"
      />
    </div>
  );
}

function ProposalModeButton({
  active,
  disabled,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  disabled: boolean;
  onClick: () => void;
  icon: ReactNode;
  label: string;
}) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn(
        "h-7 gap-1.5 rounded-full px-2.5 text-xs",
        active && "border-indigo-300 bg-indigo-100 text-indigo-700 hover:bg-indigo-200 hover:text-indigo-800 dark:border-indigo-400/50 dark:bg-indigo-950/50 dark:text-indigo-200 dark:hover:bg-indigo-900/60 dark:hover:text-indigo-100",
      )}
      disabled={disabled}
      aria-pressed={active}
      onClick={onClick}
    >
      {icon}{active ? "配置提案已开启" : label}
    </Button>
  );
}
