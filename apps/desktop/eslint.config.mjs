import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import tseslint from "typescript-eslint";

const RAW_PALETTE =
  "\\b(?:bg|text|border|ring|outline|fill|stroke|from|via|to|divide|decoration|shadow|accent|caret|placeholder)-(?:emerald|amber|zinc|sky|blue|red|green|yellow|orange|gray|slate|neutral|stone|violet|purple|indigo|rose|pink|cyan|teal|lime|fuchsia)-\\d";
const ARBITRARY_FONT_SIZE = "\\btext-\\[\\d+(?:\\.\\d+)?(?:px|rem|em)\\]";

/** 同时覆盖普通字符串（含 JSX 属性）与模板字符串片段。 */
function rawStyleSelectors() {
  return [
    [RAW_PALETTE, "不要直接使用原始色板类名，改用语义 token（text-success / bg-warning/10 / border-info/30 …）。"],
    [ARBITRARY_FONT_SIZE, "不要使用任意字号，改用主题字号（text-2xs / text-xs / text-sm …）。"],
  ].flatMap(([pattern, message]) => [
    { selector: `Literal[value=/${pattern}/]`, message },
    { selector: `TemplateElement[value.raw=/${pattern}/]`, message },
  ]);
}

const eslintConfig = defineConfig([
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      "no-undef": "off",
    },
  },
  {
    // 设计 token 守卫：颜色只能走 app/globals.css 的语义 token（success / warning /
    // info / destructive / muted …），字号只能走主题刻度（含 text-2xs）。直接写原始
    // 色板或任意 px 字号会绕过深色模式与终端岛，导致两套主题各自漂移。
    files: ["app/**/*.{ts,tsx}", "components/**/*.{ts,tsx}", "hooks/**/*.{ts,tsx}", "lib/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        ...rawStyleSelectors(),
        {
          // 界面错误文案只能经 lib/errors/user-error-message.ts 的 userErrorMessage 产生；
          // 直接取 error.message 会把库与网络层的英文原文、甚至响应体透给用户。
          selector: "ConditionalExpression[test.operator='instanceof'][test.right.name='Error'][consequent.property.name='message']",
          message: "不要把 error.message 直接用作界面文案，改用 userErrorMessage(error, 兜底文案)。",
        },
      ],
    },
  },
  {
    // 日志与错误映射模块本身需要读取原始 message（写入诊断日志 / 判定错误类别）。
    files: ["lib/logging/**", "lib/errors/**"],
    rules: { "no-restricted-syntax": ["error", ...rawStyleSelectors()] },
  },
  globalIgnores([
    // Ignore stale build output from the pre-Vite toolchain.
    ".next/**",
    "**/.next/**",
    "src-tauri/target/**",
    "out/**",
    "build/**",
    "dist/**",
  ]),
]);

export default eslintConfig;
