import { useEffect, useState } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { BackendStartupScreen } from "@/components/backend-startup-screen";
import { WorkspaceShell } from "@/components/workspace-shell";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { Button } from "@/components/ui/button";
import {
  initializeBackendRuntime,
  restartBackendRuntime,
  startBackendRuntimeMonitor,
} from "@/src/runtime-config";

type TaskRouteState = {
  returnTaskId?: number | null;
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  initialDisabledToolGroups?: string[];
  initialBanTools?: string[];
};

function TaskRoute() {
  const { pathname, state } = useLocation();
  const routeState = state as TaskRouteState | null;
  if (pathname === "/context-observer") {
    return <WorkspaceShell routeTaskId={null} contextObserverOpen />;
  }
  if (pathname === "/settings") {
    const returnTaskId = routeState?.returnTaskId;
    return <WorkspaceShell routeTaskId={returnTaskId && returnTaskId > 0 ? returnTaskId : null} settingsOpen />;
  }
  const workspaceSettingsPath = pathname.match(/^\/workspaces\/(\d+)\/settings$/);
  if (workspaceSettingsPath) {
    const workspaceId = Number(workspaceSettingsPath[1]);
    const returnTaskId = routeState?.returnTaskId;
    return Number.isInteger(workspaceId) && workspaceId > 0
      ? <WorkspaceShell
          routeTaskId={returnTaskId && returnTaskId > 0 ? returnTaskId : null}
          workspaceConfigurationId={workspaceId}
        />
      : <div className="p-6 text-sm">工作区标识无效。</div>;
  }
  const taskPath = pathname.match(/^\/tasks\/([^/]+)$/);
  if (!taskPath) {
    return pathname === "/"
      ? <WorkspaceShell routeTaskId={null} />
      : <div className="flex h-dvh items-center justify-center p-6 text-sm">页面不存在。</div>;
  }

  const parsedTaskId = Number(taskPath[1]);
  return Number.isInteger(parsedTaskId) && parsedTaskId > 0
    ? <WorkspaceShell
        routeTaskId={parsedTaskId}
        initialMessage={routeState?.initialMessage}
        initialAttachments={routeState?.initialAttachments}
        initialDisabledToolGroups={routeState?.initialDisabledToolGroups}
        initialBanTools={routeState?.initialBanTools}
      />
    : <div className="p-6 text-sm">任务标识无效。</div>;
}

function BootFailure({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <section className="max-w-md space-y-4 text-center">
        <div role="alert" aria-labelledby="backend-boot-failure-title" aria-describedby="backend-boot-failure-message">
          <h1 id="backend-boot-failure-title" className="text-lg font-semibold">Cosir 后端未能启动</h1>
          <p id="backend-boot-failure-message" className="text-muted-foreground text-sm">{error}</p>
        </div>
        <Button type="button" variant="outline" onClick={onRetry}>重试</Button>
      </section>
    </main>
  );
}

/**
 * 启动门禁：只负责首次就绪之前（启动页 → 就绪 / BootFailure）。
 *
 * 首次就绪后界面树始终保持挂载；运行期的 starting / failed / stopped 由
 * WorkspaceShell 内的 BackendStatusBanner 呈现，任务页、草稿和 Assistant 会话
 * 不随后端状态卸载，后端恢复后由会话原地对账。BootFailure 的重试即重启后端：
 * 此时 supervisor 已放弃启动，不存在可被中断的运行。
 */
function DesktopApp() {
  const [retryToken, setRetryToken] = useState(0);
  const [boot, setBoot] = useState<"starting" | "ready" | "failed">("starting");
  const [showApp, setShowApp] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const start = retryToken === 0
      ? initializeBackendRuntime()
      : restartBackendRuntime().then((config) => config.backendBaseUrl);
    void start
      .then(() => {
        if (active) setBoot("ready");
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "本地 Agent 后端启动失败");
        setBoot("failed");
      });
    return () => {
      active = false;
    };
  }, [retryToken]);

  useEffect(() => {
    if (boot !== "ready") return;
    return startBackendRuntimeMonitor();
  }, [boot]);

  useEffect(() => {
    if (boot !== "ready") return;
    const timeout = window.setTimeout(() => setShowApp(true), 280);
    return () => window.clearTimeout(timeout);
  }, [boot]);

  if (boot === "failed") return <BootFailure key={retryToken} error={error ?? "启动失败"} onRetry={() => { setBoot("starting"); setError(null); setRetryToken((value) => value + 1); }} />;
  if (boot !== "ready") return <BackendStartupScreen />;

  return (
    <>
      <BrowserRouter><Routes><Route path="*" element={<TaskRoute />} /></Routes></BrowserRouter>
      {!showApp && <BackendStartupScreen exiting />}
    </>
  );
}

export function App() {
  return <DesktopApp />;
}
