/**
 * ESLint flat 配置（ESLint 9 风格）。
 *
 * 覆盖 TypeScript + React + React Hooks 基础质量门禁，并接入
 * `eslint-plugin-tailwindcss` 的 `no-arbitrary-value` 规则，
 * 用于在 CI / 提交前自动拦截散写的 Tailwind 任意值
 * （如 `text-[10px]`、`max-w-[85%]`、`p-[7px]`），
 * 落实 `rules/Agent客户端代码开发规范.md` 第九节「设计令牌与样式纪律」。
 *
 * 规则生效范围（实测确认，非声明）：
 * - `no-arbitrary-value` 能拦截 `text-[10px]` / `max-w-[85%]` / `p-[7px]` 等任意值写法；
 * - 不能拦截 `max-w-3xl` 等具名预设（具名预设是 Tailwind 一等公民，本规则放行，此类统一靠 `tokens.ts` 收口）；
 * - 令牌定义文件（tokens.ts / messageTypography.ts）作为任意值的唯一合法源头，列入白名单豁免本规则。
 *
 * 注意：本配置仅做静态 lint，不替代 tsc 类型检查；两者在 `lint` / `build` 中分别运行。
 *
 * @module eslint.config
 */

import js from "@eslint/js";
import tseslint from "typescript-eslint";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import tailwind from "eslint-plugin-tailwindcss";

export default [
  {
    ignores: [
      "dist/**",
      "src-tauri/**",
      "node_modules/**",
      // 配置文件使用 Node CJS 风格（require 等），不纳入前端 lint 范围
      "tailwind.config.ts",
      "postcss.config.js",
      "vite.config.ts",
      "vitest.config.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
        project: false,
      },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
      tailwind,
    },
    settings: {
      react: { version: "detect" },
      // 声明 className 组合函数的调用名，使 no-arbitrary-value 能解析 `cn(...)` / `cva(...)` 内的任意值。
      // 不配置此项时，规则只检查字面量 className="..."，对全项目最主流的 cn() 写法完全漏检（假绿灯）。
      tailwindcss: { callees: ["cn", "cva"] },
    },
    rules: {
      // 项目使用 @vitejs/plugin-react 的 automatic JSX runtime，无需显式 import React
      "react/react-in-jsx-scope": "off",
      "react/jsx-uses-react": "off",
      ...reactHooks.configs.recommended.rules,
      // 禁止空 catch 块（落实规范第十节「空 catch 块」禁止项）；仅完全空白块报错，
      // 带注释/带错误处理的 catch 不在此列——但规范要求失败必须 logError，空 catch 一律违规。
      "no-empty": ["error", { allowEmptyCatch: false }],
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
      // 拦截 Tailwind 任意值散写（text-[Npx] / max-w-[N%] / p-[Npx] 等），落实 §9.2 反魔法值
      "tailwind/no-arbitrary-value": "error",
    },
  },
  {
    // 令牌定义文件是任意值的唯一合法源头，豁免 no-arbitrary-value，但其余规则仍生效
    files: [
      "**/components/ui/tokens.ts",
      "**/components/chat/messageTypography.ts",
    ],
    rules: {
      "tailwind/no-arbitrary-value": "off",
    },
  },
];
