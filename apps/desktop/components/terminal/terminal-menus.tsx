import { ChevronDownIcon, HistoryIcon } from "lucide-react";
import { ContextMenu, DropdownMenu } from "radix-ui";
import { useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { isMac } from "@/lib/platform";
import type { TerminalShell } from "@/lib/terminal/terminal-api";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { cn } from "@/lib/utils";

import type { TerminalView } from "./terminal-view";

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
              title={shell.integrationSupported ? undefined : "该 shell 无集成：不支持「命令返回」等待、命令输出区间、退出码与 cwd 跟踪，Agent 也无法往这里填入命令；按行读取不受影响"}
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
              <span className="flex-1 truncate">{session.shellKind}</span>
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

const SHORTCUTS = isMac
  ? { copy: "⌘C", paste: "⌘V", find: "⌘F" }
  : { copy: "Ctrl+C", paste: "Ctrl+V", find: "Ctrl+F" };

/**
 * 终端窗格的右键菜单：复制、粘贴、查找——低频操作的唯一落点，不在页头加按钮。两个平台行为一致：
 * 不采用 Windows Terminal 的「右键即粘贴」，终端里的命令常来自模型，误粘贴即执行的代价更高。
 *
 * 操作对象是打开菜单时的活动会话（`getView`）。选区在打开时读：macOS 上 xterm 先在右键处选中一个词再
 * 弹菜单（与 VS Code 一致）。菜单关闭后焦点交回终端；选「查找」时等菜单关闭后才打开查找条——菜单开着时
 * 它的焦点陷阱会把查找条抢到的焦点拉回菜单。前台程序开了鼠标上报（htop、tmux、`mouse=a` 的 vim）时右键
 * 归程序，不弹菜单（在右键那一刻读，模式随程序进出而变）。不做键盘唤起：Shift+F10 与菜单键在终端里属于 shell。
 */
export function TerminalContextMenu({
  getView,
  disabled,
  connected,
  onOpenSearch,
  children,
}: {
  getView: () => TerminalView | undefined;
  /** 没有可见会话（只有占位提示）时不接管右键。 */
  disabled: boolean;
  connected: boolean;
  onOpenSearch: () => void;
  children: ReactNode;
}) {
  const [hasSelection, setHasSelection] = useState(false);
  const searchAfterClose = useRef(false);
  return (
    <ContextMenu.Root onOpenChange={(open) => {
      if (open) setHasSelection(getView()?.hasSelection() ?? false);
    }}
    >
      <ContextMenu.Trigger
        asChild
        disabled={disabled}
        // 先于菜单的处理执行：取消默认行为即不打开菜单。
        onContextMenu={(event) => {
          if (getView()?.mouseReporting()) event.preventDefault();
        }}
      >
        {children}
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content
          className={TERMINAL_MENU_CONTENT_CLASS}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (searchAfterClose.current) {
              searchAfterClose.current = false;
              onOpenSearch();
            } else {
              getView()?.focus();
            }
          }}
        >
          <ContextMenu.Item className={TERMINAL_MENU_ITEM_CLASS} disabled={!hasSelection} onSelect={() => void getView()?.copySelection()}>
            <span className="flex-1">复制</span>
            <span className="text-muted-foreground text-2xs">{SHORTCUTS.copy}</span>
          </ContextMenu.Item>
          <ContextMenu.Item className={TERMINAL_MENU_ITEM_CLASS} disabled={!connected} onSelect={() => void getView()?.pasteFromClipboard()}>
            <span className="flex-1">粘贴</span>
            <span className="text-muted-foreground text-2xs">{SHORTCUTS.paste}</span>
          </ContextMenu.Item>
          <ContextMenu.Item
            className={TERMINAL_MENU_ITEM_CLASS}
            onSelect={() => {
              searchAfterClose.current = true;
            }}
          >
            <span className="flex-1">查找</span>
            <span className="text-muted-foreground text-2xs">{SHORTCUTS.find}</span>
          </ContextMenu.Item>
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
