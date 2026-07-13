/**
 * Vite 应用入口。
 *
 * 负责挂载 React 根组件到 DOM。
 *
 * @module main
 */

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
