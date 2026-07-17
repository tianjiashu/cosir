import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@shared": resolve(__dirname, "../../apps/shared/ts"),
      "@": resolve(__dirname, "src"),
    },
  },
  // Tauri 在开发模式下期望使用固定端口
  server: {
    port: 1420,
    strictPort: true,
    // 配置后端 API 代理（开发环境）
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/tasks": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/turns": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/workspaces": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/logs": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/runs": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/approvals": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/health": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/traces": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/replay": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  // 环境变量前缀安全限制
  envPrefix: ["VITE_", "TAURI_"],
  build: {
    target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari14",
    minify: !process.env.TAURI_ENV_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
});
