import React from "react";
import ReactDOM from "react-dom/client";
import "@/styles/globals.css";
import { RootErrorBoundary } from "@/components/render-isolation/root-error-boundary";
import { frontendLog } from "@/lib/logging/frontend-log";
import { installGlobalFrontendErrorHandlers } from "@/lib/logging/global-error-handlers";
import { titlebarMode } from "@/lib/platform";
import { initHighlighting } from "@/lib/syntax/highlighter";
import { installThemePreferenceSync } from "@/lib/theme/theme-preference";
import { App } from "./App";

installGlobalFrontendErrorHandlers();
installThemePreferenceSync();
// 首帧前打上标题栏形态，globals.css 据此给出左右安全区，避免先按无安全区渲染再跳动。
document.documentElement.dataset.titlebar = titlebarMode;

const root = ReactDOM.createRoot(document.getElementById("root")!, {
  onRecoverableError(error, errorInfo) {
    void frontendLog("ERROR", "react_recoverable_render_error", "React 并发渲染发生可恢复异常", {
      data: {
        componentStack: errorInfo.componentStack?.trim().slice(0, 4096) || null,
      },
      error,
    });
  },
});

// 着色引擎（WASM）先就绪，之后着色全部同步，Markdown 编辑器挂载即着色；初始化失败只记日志，全应用按原文显示
// （见 `lib/syntax/highlighter.ts`）。
void initHighlighting()
  .catch((error: unknown) => frontendLog("ERROR", "highlight_engine_init_failed", "代码着色引擎初始化失败", { error }))
  .finally(() => root.render(
    <React.StrictMode>
      <RootErrorBoundary>
        <App />
      </RootErrorBoundary>
    </React.StrictMode>,
  ));
