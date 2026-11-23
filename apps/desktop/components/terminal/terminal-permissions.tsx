import { useState } from "react";
import { useAuiState } from "@assistant-ui/react";
import type { TerminalApproval, TerminalPermissionMode } from "@/lib/assistant/contract";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";
import { decideTerminalApproval, setTerminalPermission } from "@/lib/assistant/session-commands";
import { useSandboxCapability } from "@/lib/terminal/sandbox-capability";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { Button } from "@/components/ui/button";

const EMPTY_APPROVALS: TerminalApproval[] = [];

/**
 * 会话的终端权限：事实在后端会话上（snapshot `terminal_permission_mode`），运行中也可切换，Agent 的下一条命令
 * 生效。本机确定不支持沙箱时「请求批准」不可选（后端同样拒绝）。
 */
export function TerminalPermissionSelector({ sessionId }: { sessionId: number }) {
  const mode = useAuiState((s) => readRuntimeTransportState(s.thread.state)?.terminal_permission_mode ?? null);
  const { capability, error: probeError } = useSandboxCapability();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const unsupported = capability?.settled === true && !capability.supported;
  const choose = async (value: TerminalPermissionMode) => {
    setBusy(true);
    setError("");
    try {
      await setTerminalPermission(sessionId, value);
    } catch (e) {
      setError(userErrorMessage(e, "无法切换终端权限"));
    } finally {
      setBusy(false);
    }
  };
  const hint = error || probeError || (unsupported ? "本机不支持终端沙箱" : "仅影响 Agent 发起的终端命令，下一条命令生效");
  if (mode === null) return null;
  return (
    <div className="flex min-w-0 items-center gap-1">
      <select
        aria-label="终端权限"
        value={mode}
        disabled={busy}
        title={hint}
        className="max-w-36 rounded-md bg-transparent px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
        onChange={(e) => void choose(e.target.value as TerminalPermissionMode)}
      >
        <option value="request_approval" disabled={unsupported}>请求批准{unsupported ? "（本机不支持）" : ""}</option>
        <option value="full_access">完全访问权限</option>
      </select>
      {(error || unsupported) && <span role={error ? "alert" : undefined} className="max-w-56 text-xs text-muted-foreground">{error || "本机不支持终端沙箱"}</span>}
    </div>
  );
}

export function TerminalApprovalCards({ sessionId }: { sessionId: number }) {
  const requests = useAuiState((s) => readRuntimeTransportState(s.thread.state)?.terminal_approvals ?? EMPTY_APPROVALS);
  return <div className="space-y-3">{requests.map((request) => <TerminalApprovalCard key={request.id} sessionId={sessionId} request={request} />)}</div>;
}

function TerminalApprovalCard({ sessionId, request }: { sessionId: number; request: TerminalApproval }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    setError("");
    try {
      await decideTerminalApproval(sessionId, request.id, decision);
    } catch (e) {
      setError(userErrorMessage(e, "无法提交终端批准"));
      setBusy(false);
    }
  };
  return (
    <section aria-label="终端执行批准" className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4 text-sm">
      <div className="mb-2 font-medium">终端请求批准{request.originSessionId !== sessionId ? ` · ${request.originTitle}` : ""}</div>
      <p className="mb-2">
        这条命令将升级运行：可联网（含 localhost 与监听端口），可写你有权限的任何位置（包括启动项与其他程序）；只有 Cosir 的数据与程序受保护。只对这一次有效。
      </p>
      {request.intent && <p className="mb-2 text-muted-foreground">{request.intent}</p>}
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md bg-background p-3 text-xs">{request.command}</pre>
      <dl className="my-3 space-y-1 break-all text-xs">
        <div>Shell：{request.shell}</div>
        <div>工作目录：{request.cwd}</div>
      </dl>
      <div className="flex gap-2">
        <Button size="sm" disabled={busy} onClick={() => void decide("approve")}>允许一次</Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide("reject")}>拒绝</Button>
      </div>
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </section>
  );
}
