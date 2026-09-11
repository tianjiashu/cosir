import React from "react";
import ReactDOM from "react-dom/client";
import "@/app/globals.css";
import { RootErrorBoundary } from "@/components/root-error-boundary";
import { installGlobalFrontendErrorHandlers } from "@/lib/logging/global-error-handlers";
import { App } from "./App";

installGlobalFrontendErrorHandlers();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </React.StrictMode>,
);
