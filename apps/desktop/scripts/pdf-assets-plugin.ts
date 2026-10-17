import fs from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

/** PDF 字体、字符映射与 WASM 随桌面包离线发布；开发服务只暴露明确的资源清单。 */
export function pdfAssetsPlugin(): Plugin {
  const assets = new Map<string, string>();
  const base = path.resolve(__dirname, "../node_modules/pdfjs-dist");
  for (const directory of ["cmaps", "standard_fonts", "wasm"]) {
    for (const file of fs.readdirSync(path.join(base, directory))) {
      const absolute = path.join(base, directory, file);
      if (fs.statSync(absolute).isFile())
        assets.set(`pdf-assets/${directory}/${file}`, absolute);
    }
  }
  return {
    name: "cosir-pdf-assets",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const file = assets.get(
          (request.url ?? "").split("?")[0].replace(/^\//, ""),
        );
        if (!file) return next();
        response.setHeader(
          "Content-Type",
          file.endsWith(".wasm")
            ? "application/wasm"
            : "application/octet-stream",
        );
        fs.createReadStream(file).pipe(response);
      });
    },
    generateBundle() {
      for (const [fileName, source] of assets)
        this.emitFile({
          type: "asset",
          fileName,
          source: fs.readFileSync(source),
        });
    },
  };
}
