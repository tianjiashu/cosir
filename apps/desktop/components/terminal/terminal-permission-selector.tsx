import { useState } from "react";
import { useAuiState } from "@assistant-ui/react";
import { ShieldAlertIcon, ShieldCheckIcon } from "lucide-react";

import { ComposerMenuContent, ComposerMenuRow } from "@/components/composer/composer-menu";
import { COMPOSER_CONFIG_BUTTON_CLASS_NAME } from "@/components/composer/composer-surface";
import { Command, CommandList } from "@/components/ui/command";
import { Popover, PopoverTrigger } from "@/components/ui/popover";
import type { TerminalPermissionMode } from "@/lib/assistant/contract";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { setTerminalPermission } from "@/lib/assistant/session-commands";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useSandboxCapability } from "@/lib/terminal/sandbox-capability";
import { cn } from "@/lib/utils";

const MODE_TITLES: Record<TerminalPermissionMode, string> = {
  request_approval: "请求批准",
  full_access: "完全访问权限",
};

/**
 * 会话的终端权限：事实在后端会话上（snapshot `terminal_permission_mode`），运行中也可切换，Agent 的下一条命令
 * 生效。安全设置要让人看得懂，所以是带说明的菜单而不是原生下拉；完全访问权限时触发按钮用警示色。
 * 本机确定不支持沙箱时「请求批准」不可选（后端同样拒绝），说明里写原因。
 */
export function TerminalPermissionSelector({ sessionId }: { sessionId: number }) {
  const mode = useAuiState((s) => readRuntimeTransportState(s.thread.state)?.terminal_permission_mode ?? null);
  const { capability, error: probeError } = useSandboxCapability();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const unsupported = capability?.settled === true && !capability.supported;
  if (mode === null) return null;

  const choose = async (value: TerminalPermissionMode) => {
    setOpen(false);
    if (value === mode) return;
    setBusy(true);
    try {
      await setTerminalPermission(sessionId, value);
    } catch (error) {
      void frontendLog("WARNING", "terminal_permission_change_failed", "切换终端权限失败", { data: { sessionId, mode: value }, error });
      notify.error("无法切换终端权限", { error, fallback: "请稍后重试" });
    } finally {
      setBusy(false);
    }
  };
  const approvalDescription = unsupported
    ? "本机不支持终端沙箱"
    : probeError || "沙箱内直接运行，越界的命令先问你";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        aria-label={`终端权限：${MODE_TITLES[mode]}`}
        title="仅影响 Agent 发起的终端命令，下一条命令生效"
        disabled={busy}
        className={cn(COMPOSER_CONFIG_BUTTON_CLASS_NAME, mode === "full_access" && "text-warning hover:text-warning aria-expanded:text-warning")}
      >
        {MODE_TITLES[mode]}
      </PopoverTrigger>
      <ComposerMenuContent align="end">
        <Command aria-label="终端权限" defaultValue={MODE_TITLES[mode]} className="outline-none">
          <CommandList className="p-1">
            <ComposerMenuRow
              icon={<ShieldCheckIcon />}
              title={MODE_TITLES.request_approval}
              description={approvalDescription}
              checked={mode === "request_approval"}
              disabled={unsupported}
              onSelect={() => void choose("request_approval")}
            />
            <ComposerMenuRow
              icon={<ShieldAlertIcon />}
              title={MODE_TITLES.full_access}
              description="命令不受沙箱限制，可联网、可写工作区外"
              checked={mode === "full_access"}
              onSelect={() => void choose("full_access")}
            />
          </CommandList>
        </Command>
      </ComposerMenuContent>
    </Popover>
  );
}
