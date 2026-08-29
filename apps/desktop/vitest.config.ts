import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@shared": resolve(__dirname, "../shared/ts"),
      "@": resolve(__dirname, "src"),
    },
  },
  // 强制 NODE_ENV=development，避免 vite optimizeDeps 将 react 预构建为 production 版，
  // 否则 @testing-library/react 的 act(...) 会报 "not supported in production builds of React"。
  define: {
    "process.env.NODE_ENV": JSON.stringify("development"),
  },
  test: {
    environment: "node",
    globals: true,
    include: ["src/tests/**/*.test.ts", "src/tests/**/*.test.tsx"],
    // 模拟 import.meta.env.DEV，logger 在 dev 环境输出 console
    env: {
      DEV: "true",
    },
    // 修复验证测试（fixverify_*）需要 mock `react` 以在非组件上下文直接调用 hook，
    // 将 zustand 排除出依赖预构建，确保其对 `react` 的引用能被 vi.mock("react") 拦截。
    server: {
      deps: {
        inline: ["zustand"],
      },
    },
  },
});
