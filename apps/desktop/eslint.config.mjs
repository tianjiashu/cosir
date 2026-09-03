import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import tseslint from "typescript-eslint";

const eslintConfig = defineConfig([
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      "no-undef": "off",
    },
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
