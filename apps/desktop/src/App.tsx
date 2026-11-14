import { useEffect, useState } from "react";
import { RouterProvider } from "react-router-dom";
import { BackendStartupScreen } from "@/components/backend/startup-screen";
import { QueryClientProvider } from "@tanstack/react-query";
import { ConfirmHost } from "@/components/confirm/confirm-host";
import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { createQueryClient } from "@/lib/query/query-client";
import { useBackendQuerySync } from "@/lib/query/use-backend-query-sync";
import {
  initializeBackendRuntime,
  restartBackendRuntime,
} from "@/lib/backend/runtime-config";
import { createAppRouter } from "@/src/router";

/** 路由在首次就绪后才创建：创建即开始监听 history，启动门禁期间不需要它。 */
function AppRouter() {
  const [router] = useState(createAppRouter);
  return <RouterProvider router={router} />;
}

function BootFailure({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <main className="app-drag-region flex min-h-dvh items-center justify-center p-6">
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
 * AppLayout 内的 BackendStatusBanner 呈现，会话页、草稿和 Assistant 会话
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
      : restartBackendRuntime();
    void start
      .then(() => {
        if (active) setBoot("ready");
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(userErrorMessage(cause, "本地 Agent 后端启动失败"));
        setBoot("failed");
      });
    return () => {
      active = false;
    };
  }, [retryToken]);

  useEffect(() => {
    if (boot !== "ready") return;
    const timeout = window.setTimeout(() => setShowApp(true), 280);
    return () => window.clearTimeout(timeout);
  }, [boot]);

  if (boot === "failed") return <BootFailure key={retryToken} error={error ?? "启动失败"} onRetry={() => { setBoot("starting"); setError(null); setRetryToken((value) => value + 1); }} />;

  // 启动遮罩在同一位置从 starting 保持到退场结束，不因 ready 重新挂载（入场动画与等待计时不会重来）。
  return (
    <>
      {boot === "ready" && <AppRouter />}
      {!showApp && <BackendStartupScreen exiting={boot === "ready"} />}
    </>
  );
}

function BackendQuerySync() {
  useBackendQuerySync();
  return null;
}

/**
 * 应用根：全局只挂载一次的宿主——页面级查询缓存、提示的延迟与分组、toast、确认框。
 * 它们在启动门禁之外，启动失败页与主界面共用同一套通知与确认通道。
 */
export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <BackendQuerySync />
        <DesktopApp />
        <ConfirmHost />
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
