/**
 * Vite 应用入口。
 *
 * 负责挂载 React 根组件到 DOM。
 *
 * @module main
 */

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { logError } from "./lib/logger";
import { ErrorBoundary } from "./components/ErrorBoundary";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("无法找到 #root 挂载点，请检查 index.html");
}

createRoot(rootElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);

// 全局兜底：把未捕获的同步/异步错误记录到日志，便于在 Tauri WebView 中排查白屏
window.addEventListener("error", (event) => {
  logError("未捕获的全局错误", event.error, { module: "main", source: event.filename });
});
window.addEventListener("unhandledrejection", (event) => {
  logError("未处理的 Promise 拒绝", event.reason, { module: "main" });
});
