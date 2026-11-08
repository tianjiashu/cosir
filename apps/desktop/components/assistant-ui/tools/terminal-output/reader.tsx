import { useRef, useState } from "react";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import type { TerminalLinkOptions } from "@/components/terminal/terminal-links";
import { useDisclosureStore } from "@/lib/assistant/disclosure-store";
import { TerminalViewport, type TerminalViewportHandle } from "./viewport";

/** 下载后端累计原文；不把有限的 xterm 屏幕缓冲伪称为完整输出。 */
function downloadTerminalOutput(output: string) {
  const url = URL.createObjectURL(new Blob([output], { type: "text/plain;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "terminal-output.txt";
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 阅读位置属于 Task UI，-1 表示跟随最新；不会写入后端执行事实。 */
export function TerminalOutputReader({ taskId, toolCallId, output, links, open, onOpenChange, command, statusLabel, finalFocus }: {
  taskId: number; toolCallId: string; output: string; links: TerminalLinkOptions;
  open: boolean; onOpenChange: (open: boolean) => void; command?: string; statusLabel: string; finalFocus?: () => HTMLElement | boolean;
}) {
  const viewport = useRef<TerminalViewportHandle>(null);
  const [truncated, setTruncated] = useState(false);
  const [pausedAtOutput, setPausedAtOutput] = useState<string | null>(null);
  const position = useDisclosureStore.getState().terminalPositions[taskId]?.[toolCallId];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent finalFocus={finalFocus} className="flex h-[min(80vh,800px)] w-[min(1100px,calc(100vw-2rem))] max-w-none flex-col overflow-hidden sm:max-w-none">
        <DialogTitle>命令输出</DialogTitle>
        <DialogDescription className="sr-only">只读终端输出。向上滚动暂停跟随，回到最新继续跟随。</DialogDescription>
        {command && <div className="flex min-w-0 shrink-0 items-start gap-2">
          <pre className="line-clamp-2 min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-xs" title={command}>{command}</pre>
          <CopyIconButton showLabel label="复制命令" getText={() => command} logEvent="terminal_command_copy_failed" logData={{ taskId, toolCallId }} />
        </div>}
        <div className="flex shrink-0 flex-wrap items-center gap-2 pr-8">
          <span className="text-muted-foreground text-xs">{statusLabel}</span>
          <CopyIconButton showLabel label={truncated ? "复制保留内容" : "复制输出"} getText={() => viewport.current?.copyText() ?? ""} logEvent="terminal_output_copy_failed" logData={{ taskId, toolCallId }} />
          <Button variant="ghost" size="sm" onClick={() => downloadTerminalOutput(output)}>下载原始输出</Button>
          {pausedAtOutput !== null && pausedAtOutput !== output && <Button variant="outline" size="sm" onClick={() => viewport.current?.scrollToBottom()}>有新输出 · 回到最新</Button>}
        </div>
        {truncated && <p className="text-muted-foreground text-xs">较早内容已移出显示缓冲；下载可获取当前已收集的全部原始输出（含控制字符）。</p>}
        <div className="bg-surface min-h-0 flex-1 rounded-lg p-2" data-terminal-reader="true">
          <TerminalViewport ref={viewport} output={output} links={links} initialLine={position} onTruncatedChange={setTruncated}
            onPositionChange={(line, bottom) => {
              if (!open) return;
              useDisclosureStore.getState().setTerminalPosition(taskId, toolCallId, bottom ? -1 : line);
              setPausedAtOutput((previous) => bottom ? null : previous ?? output);
            }} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
