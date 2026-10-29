import { BotIcon, PlusIcon, UserIcon, XIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { panelTabClass } from "@/components/ui/panel-tokens";
import type { TerminalShell } from "@/lib/terminal/terminal-api";
import { isActiveTerminalSession, terminalStatusPresentation, type TerminalTab } from "@/lib/terminal/terminal-sessions";
import { cn } from "@/lib/utils";

import { TerminalShellMenu } from "./terminal-menus";

/** 终端面板头部左侧：会话标签、「+」（默认 shell 新建）与选 shell 新建的下拉。 */
export function TerminalTabBar({
  tabs,
  activeSessionId,
  shells,
  shellsLoading,
  createDisabled,
  onSelect,
  onClose,
  onCreate,
}: {
  tabs: readonly TerminalTab[];
  activeSessionId: string | null;
  shells: readonly TerminalShell[] | null;
  shellsLoading: boolean;
  createDisabled: boolean;
  onSelect: (sessionId: string) => void;
  onClose: (tab: TerminalTab) => void;
  onCreate: (shell: string) => void;
}) {
  const defaultShell = shells?.find((shell) => shell.isDefault)?.kind ?? null;
  return (
    <>
      <div role="tablist" aria-label="终端会话" className="flex min-w-0 items-center gap-1 overflow-x-auto">
        {tabs.map((tab) => (
          <TerminalTabButton
            key={tab.session.sessionId}
            tab={tab}
            active={tab.session.sessionId === activeSessionId}
            showShell={defaultShell !== null && tab.session.shellKind !== defaultShell}
            onSelect={() => onSelect(tab.session.sessionId)}
            onClose={() => onClose(tab)}
          />
        ))}
      </div>
      <TooltipIconButton
        tooltip="新建终端"
        aria-label="新建终端"
        className="text-muted-foreground size-7 shrink-0"
        disabled={createDisabled}
        onClick={() => onCreate("auto")}
      >
        <PlusIcon aria-hidden="true" />
      </TooltipIconButton>
      <TerminalShellMenu shells={shells} shellsLoading={shellsLoading} disabled={createDisabled} onCreate={onCreate} />
    </>
  );
}

function TerminalTabButton({
  tab,
  active,
  showShell,
  onSelect,
  onClose,
}: {
  tab: TerminalTab;
  active: boolean;
  showShell: boolean;
  onSelect: () => void;
  onClose: () => void;
}) {
  const { session } = tab;
  const status = terminalStatusPresentation(session.status);
  const CreatorIcon = session.createdBy === "agent" ? BotIcon : UserIcon;
  return (
    <div className={panelTabClass(active, "max-w-56")} data-terminal-tab={session.sessionId}>
      <button
        type="button"
        role="tab"
        aria-selected={active}
        className="flex min-w-0 items-center gap-1.5 outline-none"
        onClick={onSelect}
        title={`${tab.title} · ${session.shellKind} · ${status.label} · ${session.createdBy === "agent" ? "Agent 创建" : "用户创建"}`}
      >
        <span className={cn("size-1.5 shrink-0 rounded-full", status.dot)} aria-label={status.label} />
        <CreatorIcon className="size-3 shrink-0 opacity-70" aria-label={session.createdBy === "agent" ? "Agent 创建" : "用户创建"} />
        <span className="truncate">{tab.title}</span>
        {showShell && <span className="text-muted-foreground shrink-0 font-mono text-2xs">{session.shellKind}</span>}
      </button>
      <TooltipIconButton
        tooltip={isActiveTerminalSession(session) ? "关闭终端" : "从标签栏移除"}
        aria-label={`关闭 ${tab.title}`}
        className="size-5 shrink-0 opacity-60 hover:opacity-100"
        onClick={onClose}
      >
        <XIcon aria-hidden="true" />
      </TooltipIconButton>
    </div>
  );
}
