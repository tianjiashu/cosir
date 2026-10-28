import { ChevronDownIcon, EllipsisVerticalIcon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import type { TerminalShell } from "@/lib/terminal/terminal-api";
import type { TerminalConnectionState } from "@/lib/terminal/terminal-connection";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { cn } from "@/lib/utils";

const TERMINAL_MENU_CONTENT_CLASS = cn(MENU_CONTENT_CLASS, "min-w-48 max-w-[calc(100vw-1rem)] text-popover-foreground");
const TERMINAL_MENU_ITEM_CLASS = cn(
  MENU_ITEM_CLASS,
  "flex items-center gap-2 select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
);

export const TERMINAL_CONNECTION_LABEL: Record<TerminalConnectionState, string> = {
  connecting: "连接中",
  connected: "已连接",
  reconnecting: "重新连接中…",
  ended: "会话已结束",
  expired: "会话已失效",
  failed: "连接失败",
};

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

/** ⋮ 菜单：已结束会话（查看最后画面）、当前会话的连接状态与重连。 */
export function TerminalMoreMenu({
  ended,
  connection,
  onOpenEnded,
  onReconnect,
}: {
  ended: TerminalSessionEntry[];
  connection: TerminalConnectionState | null;
  onOpenEnded: (sessionId: string) => void;
  onReconnect?: () => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button size="icon-sm" variant="ghost" className="text-muted-foreground size-7" aria-label="更多终端操作" title="更多">
          <EllipsisVerticalIcon />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" className={TERMINAL_MENU_CONTENT_CLASS}>
          <DropdownMenu.Sub>
            <DropdownMenu.SubTrigger disabled={ended.length === 0} className={TERMINAL_MENU_ITEM_CLASS}>
              已结束 ({ended.length})
            </DropdownMenu.SubTrigger>
            <DropdownMenu.Portal>
              <DropdownMenu.SubContent className={TERMINAL_MENU_CONTENT_CLASS}>
                {ended.map((session) => (
                  <DropdownMenu.Item key={session.sessionId} className={TERMINAL_MENU_ITEM_CLASS} onSelect={() => onOpenEnded(session.sessionId)}>
                    <span className="flex-1 truncate">{session.label.trim() || session.shellKind}</span>
                    <span className="text-muted-foreground text-2xs">
                      {terminalStatusPresentation(session.status).label}{session.exitCode !== null ? ` · ${session.exitCode}` : ""}
                    </span>
                  </DropdownMenu.Item>
                ))}
                <div className="text-muted-foreground px-2 py-1 text-2xs">只能查看缓冲内的最后画面，可能不完整</div>
              </DropdownMenu.SubContent>
            </DropdownMenu.Portal>
          </DropdownMenu.Sub>
          <DropdownMenu.Separator className="bg-border my-1 h-px" />
          <DropdownMenu.Label className="text-muted-foreground px-2 py-1 text-2xs">
            连接：{connection ? TERMINAL_CONNECTION_LABEL[connection] : "未连接"}
          </DropdownMenu.Label>
          <DropdownMenu.Item
            disabled={!onReconnect || connection === "connected" || connection === "ended" || connection === "expired"}
            className={TERMINAL_MENU_ITEM_CLASS}
            onSelect={() => onReconnect?.()}
          >
            重新连接
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
