import { pdfAssetsPlugin } from "./scripts/pdf-assets-plugin";
import { defineConfig, type Plugin } from "vite";
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
  "stringify-entities",
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
  return packageName === "parse5" || packageName === "streamdown" || packageName.startsWith("@streamdown/")
    || packageName === "marked" || packageName === "katex" || packageName === "mermaid"
    || packageName.startsWith("@mermaid-js/") || packageName === "shiki" || packageName.startsWith("@shikijs/")
    || MARKDOWN_PACKAGE_PREFIXES.some((prefix) => packageName.startsWith(prefix));
}

function getVendorChunk(packageName: string): string | undefined {
  if (packageName.startsWith("@codemirror/lang-") && packageName !== "@codemirror/lang-markdown") return undefined;
  if (packageName === "@codemirror/language-data") return undefined;
  if (["react", "react-dom", "scheduler", "use-sync-external-store"].includes(packageName)) {
    return "vendor-react";
  }
  if (["@assistant-ui/react-markdown", "@assistant-ui/react-streamdown"].includes(packageName)) {
    return "vendor-markdown";
  }
  if (packageName.startsWith("@assistant-ui/") || packageName === "assistant-stream") {
    return "vendor-assistant-ui";
  }
  if (packageName === "pdfjs-dist") return "vendor-pdf";
  if (packageName === "@react-symbols/icons") return "vendor-file-icons";
  if (isMarkdownPackage(packageName)) return "vendor-markdown";
  if (["react-diff-view", "diff"].includes(packageName)) return "vendor-content";
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

/**
 * 开发服务器按端口各用一个依赖预构建缓存目录（`node_modules/.vite/port-<端口>`）。
 *
 * 同一项目常同时跑多个 Vite（Electron 开发 3000、各 E2E 槽位 4173+、预览服务器），共用默认的
 * `node_modules/.vite` 时，后启动或重新预构建的进程会覆盖别人的 `deps/`：先前的进程仍按内存里的
 * 文件名提供模块，页面里懒加载的依赖分块（Mermaid、代码高亮）请求到已被换掉的文件，得到 504，
 * 消息渲染失败。所有启动方都带 `--strictPort`，端口即进程身份，按端口分目录就不需要各启动方
 * 记得传参；同一端口重启时沿用自己的缓存。插件的 `config` 钩子拿到的是已合并命令行参数的配置。
 */
const DEV_SERVER_PORT = 3000;

function cacheDirPerDevServerPort(): Plugin {
  return {
    name: "cosir:cache-dir-per-dev-server-port",
    apply: "serve",
    config(config) {
      const port = config.server?.port ?? DEV_SERVER_PORT;
      return { cacheDir: path.resolve(__dirname, "node_modules", ".vite", `port-${port}`) };
    },
  };
}

export default defineConfig(({ command }) => {
  // Vite 决定 NODE_ENV 的规则是 `process.env.NODE_ENV || mode`，且仅在未设置时才回退到
  // 默认值；因此外部环境（IDE/终端）注入的 NODE_ENV=production 会让 dev 被当作生产模式：
  // 依赖预构建会产出生产版 React（react-jsx-dev-runtime 的 jsxDEV 被置空），页面启动即抛
  // `_jsxDEV is not a function` 而白屏。这里按命令语义归一 NODE_ENV（serve=development、
  // build=production），使构建结果不再受外部环境污染。
  process.env.NODE_ENV = command === "serve" ? "development" : "production";

  return {
    plugins: [react(), pdfAssetsPlugin(), cacheDirPerDevServerPort()],
    resolve: {
      alias: { "@": path.resolve(__dirname, ".") },
    },
    server: {
      host: "127.0.0.1",
      port: DEV_SERVER_PORT,
      strictPort: true,
    },
    build: {
      outDir: path.resolve(__dirname, "..", "..", "target", "frontend"),
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
  };
});
