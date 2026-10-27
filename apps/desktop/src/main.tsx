import React from "react";
import ReactDOM from "react-dom/client";
import "@/app/globals.css";
import { RootErrorBoundary } from "@/components/root-error-boundary";
import { frontendLog } from "@/lib/logging/frontend-log";
import { installGlobalFrontendErrorHandlers } from "@/lib/logging/global-error-handlers";
import { titlebarMode } from "@/lib/platform";
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
    }).catch(() => undefined);
  },
});

root.render(
  <React.StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </React.StrictMode>,
);
