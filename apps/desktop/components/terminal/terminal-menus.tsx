import { ChevronDownIcon, HistoryIcon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import type { TerminalShell } from "@/lib/terminal/terminal-api";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { cn } from "@/lib/utils";

const TERMINAL_MENU_CONTENT_CLASS = cn(MENU_CONTENT_CLASS, "min-w-48 max-w-[calc(100vw-1rem)] text-popover-foreground");
const TERMINAL_MENU_ITEM_CLASS = cn(
  MENU_ITEM_CLASS,
  "flex items-center gap-2 select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
);

/** 「+」旁的下拉：选 shell 新建终端（「+」本身直接用默认 shell）。 */
export function TerminalShellMenu({
  shells,
  shellsLoading,
  disabled,
  onCreate,
}: {
  shells: readonly TerminalShell[] | null;
  shellsLoading: boolean;
  disabled: boolean;
  onCreate: (shell: string) => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button size="icon-sm" variant="ghost" className="text-muted-foreground -ml-1 size-5 shrink-0" aria-label="选择 shell" title="选择 shell 新建终端" disabled={disabled}>
          <ChevronDownIcon />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="start" className={TERMINAL_MENU_CONTENT_CLASS}>
          {shellsLoading && <DropdownMenu.Item disabled className={TERMINAL_MENU_ITEM_CLASS}>正在读取可用 shell…</DropdownMenu.Item>}
          {shells?.map((shell) => (
            <DropdownMenu.Item
              key={shell.kind}
              className={TERMINAL_MENU_ITEM_CLASS}
              onSelect={() => onCreate(shell.kind)}
              title={shell.integrationSupported ? undefined : "该 shell 无集成：不支持「命令返回」等待、命令输出区间、退出码与 cwd 跟踪；按行读取、输入与交接不受影响"}
            >
              <span className="flex-1">{shell.label}</span>
              {shell.isDefault && <span className="text-muted-foreground text-2xs">默认</span>}
              {!shell.integrationSupported && <span className="text-warning text-2xs">功能受限</span>}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/**
 * 已结束会话列表（查看缓冲内的最后画面）。连接状态与重连在面板状态栏就地处理，不放进菜单；
 * 没有已结束会话时按钮禁用而不隐藏，页头右侧按钮位置保持稳定。
 */
export function TerminalEndedMenu({
  ended,
  onOpenEnded,
}: {
  ended: TerminalSessionEntry[];
  onOpenEnded: (sessionId: string) => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          size="icon-sm"
          variant="ghost"
          className="text-muted-foreground size-7"
          aria-label={`已结束的终端（${ended.length}）`}
          title={ended.length === 0 ? "没有已结束的终端" : `已结束的终端（${ended.length}）`}
          disabled={ended.length === 0}
        >
          <HistoryIcon />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" className={TERMINAL_MENU_CONTENT_CLASS}>
          {ended.map((session) => (
            <DropdownMenu.Item key={session.sessionId} className={TERMINAL_MENU_ITEM_CLASS} onSelect={() => onOpenEnded(session.sessionId)}>
              <span className="flex-1 truncate">{session.label.trim() || session.shellKind}</span>
              <span className="text-muted-foreground text-2xs">
                {terminalStatusPresentation(session.status).label}{session.exitCode !== null ? ` · ${session.exitCode}` : ""}
              </span>
            </DropdownMenu.Item>
          ))}
          <div className="text-muted-foreground px-2 py-1 text-2xs">只能查看缓冲内的最后画面，可能不完整</div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
