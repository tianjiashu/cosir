import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "node:path";

const MARKDOWN_PACKAGE_PREFIXES = [
  "bail",
  "ccount",
  "character-",
  "comma-separated",
  "decode-",
  "devlop",
  "entities",
  "estree-util-",
  "extend",
  "hast-",
  "hastscript",
  "html-",
  "inline-style-parser",
  "is-plain-obj",
  "longest-streak",
  "markdown-table",
  "mdast-",
  "micromark",
  "property-information",
  "rehype-",
  "remark-",
  "space-separated",
  "style-to-",
  "trim-lines",
  "trough",
  "unified",
  "unist-",
  "vfile",
  "web-namespaces",
  "zwitch",
];

function getPackageName(moduleId: string): string | null {
  const normalizedId = moduleId.replaceAll("\\", "/");
  const marker = "/node_modules/";
  const markerIndex = normalizedId.lastIndexOf(marker);
  if (markerIndex < 0) return null;
  const segments = normalizedId.slice(markerIndex + marker.length).split("/");
  return segments[0].startsWith("@") ? segments.slice(0, 2).join("/") : segments[0];
}

function isMarkdownPackage(packageName: string): boolean {
  return packageName === "parse5" || packageName === "streamdown" || packageName === "marked"
    || MARKDOWN_PACKAGE_PREFIXES.some((prefix) => packageName.startsWith(prefix));
}

function getVendorChunk(packageName: string): string {
  if (["react", "react-dom", "scheduler", "use-sync-external-store"].includes(packageName)) {
    return "vendor-react";
  }
  if (packageName.startsWith("@assistant-ui/") || packageName === "assistant-stream") {
    return "vendor-assistant-ui";
  }
  if (isMarkdownPackage(packageName)) return "vendor-markdown";
  if (["react-diff-view", "diff"].includes(packageName)) return "vendor-content";
  if (packageName.startsWith("@tauri-apps/")) return "vendor-tauri";
  if (packageName === "react-router" || packageName === "react-router-dom" || packageName === "@remix-run/router") {
    return "vendor-router";
  }
  if (
    packageName.startsWith("@base-ui/") ||
    packageName.startsWith("@floating-ui/") ||
    packageName.startsWith("@radix-ui/") ||
    packageName.startsWith("radix-ui/") ||
    packageName === "class-variance-authority" ||
    packageName === "clsx" ||
    packageName === "floating-ui" ||
    packageName === "lucide-react" ||
    packageName === "react-remove-scroll" ||
    packageName === "react-remove-scroll-bar" ||
    packageName === "react-style-singleton" ||
    packageName === "react-textarea-autosize" ||
    packageName === "tailwind-merge" ||
    packageName.startsWith("use-")
  ) {
    return "vendor-shared";
  }
  // Small uncategorised runtime helpers are kept with the shared UI boundary.
  // Keeping one owner for this dependency family prevents Rollup from creating
  // a vendor-ui <-> vendor-runtime cycle while retaining a stable cache chunk.
  return "vendor-shared";
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
  server: {
    host: "127.0.0.1",
    port: 3000,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const packageName = getPackageName(id);
          return packageName ? getVendorChunk(packageName) : undefined;
        },
      },
    },
  },
});
