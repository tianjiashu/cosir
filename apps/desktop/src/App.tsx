import { useEffect, useState } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { WorkspaceShell } from "@/components/workspace-shell";
import { initializeBackendRuntime, restartBackendRuntime } from "@/src/runtime-config";

type TaskRouteState = {
  initialMessage?: string;
};

function TaskRoute() {
  const { pathname, state } = useLocation();
  const taskPath = pathname.match(/^\/tasks\/([^/]+)$/);
  if (!taskPath) {
    return pathname === "/"
      ? <WorkspaceShell routeTaskId={null} />
      : <div className="flex h-dvh items-center justify-center p-6 text-sm">页面不存在。</div>;
  }

  const parsedTaskId = Number(taskPath[1]);
  const routeState = state as TaskRouteState | null;
  return Number.isInteger(parsedTaskId) && parsedTaskId > 0
    ? <WorkspaceShell routeTaskId={parsedTaskId} initialMessage={routeState?.initialMessage} />
    : <div className="p-6 text-sm">任务标识无效。</div>;
}

function BootFailure({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <section className="max-w-md space-y-4 text-center">
        <h1 className="text-lg font-semibold">Cosir 后端未能启动</h1>
        <p className="text-muted-foreground text-sm">{error}</p>
        <button type="button" className="underline" onClick={onRetry}>重试</button>
      </section>
    </main>
  );
}

function DesktopApp() {
  const [retryToken, setRetryToken] = useState(0);
  const [boot, setBoot] = useState<"starting" | "ready" | "failed">("starting");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const start = retryToken === 0
      ? initializeBackendRuntime()
      : restartBackendRuntime().then((config) => config.backendBaseUrl);
    void start
      .then(() => setBoot("ready"))
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "本地 Agent 后端启动失败");
        setBoot("failed");
      });
  }, [retryToken]);

  if (boot === "failed") return <BootFailure key={retryToken} error={error ?? "启动失败"} onRetry={() => { setBoot("starting"); setError(null); setRetryToken((value) => value + 1); }} />;
  if (boot !== "ready") return <main className="flex min-h-dvh items-center justify-center text-sm">本地 Agent 正在启动…</main>;
  return <BrowserRouter><Routes><Route path="*" element={<TaskRoute />} /></Routes></BrowserRouter>;
}

export function App() {
  return <DesktopApp />;
}
