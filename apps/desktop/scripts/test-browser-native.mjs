import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import electron from "electron";
import { build } from "esbuild";
import { createServer } from "vite";
import { electronBuildOptions } from "./build-electron.mjs";
import { desktopRoot } from "./project-paths.mjs";

const root = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-browser-native-"));
await build({ ...electronBuildOptions, entryPoints: {
  main: path.join(desktopRoot, "tests/desktop-e2e/browser-native/main.ts"),
  "browser-page-preload": electronBuildOptions.entryPoints["browser-page-preload"],
}, outdir: root });
fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({ name: "cosir-browser-acceptance", version: "0.1.0", main: "main.cjs" }));
console.log(`Native test evidence: ${root}`);
const fixture = path.join(root, "vite-fixture");
const dependency = path.join(fixture, "node_modules", "acceptance-dependency");
fs.mkdirSync(dependency, { recursive: true });
fs.writeFileSync(path.join(fixture, "package.json"), JSON.stringify({ type: "module", dependencies: { "acceptance-dependency": "1.0.0" } }));
fs.writeFileSync(path.join(fixture, "index.html"), '<script type="module" src="/main.js"></script>');
fs.writeFileSync(path.join(fixture, "main.js"), "import value from 'acceptance-dependency'; document.title = value;");
fs.writeFileSync(path.join(dependency, "package.json"), JSON.stringify({ name: "acceptance-dependency", version: "1.0.0", type: "module", main: "index.js" }));
fs.writeFileSync(path.join(dependency, "index.js"), "export default 'dependency-v1';");
const vite = await createServer({ configFile: false, root: fixture, cacheDir: path.join(root, "vite-cache"), server: { host: "127.0.0.1", port: 0, hmr: false }, optimizeDeps: { include: ["acceptance-dependency"], force: true } });
await vite.listen();
const viteUrl = vite.resolvedUrls.local[0];
let rebuilding = false;
const watcher = fs.watch(root, async (_event, filename) => {
  if (filename !== "rebuild-request" || rebuilding) return;
  rebuilding = true;
  try {
    fs.writeFileSync(path.join(dependency, "index.js"), "export default 'dependency-v2';");
    await vite.restart(true);
    fs.writeFileSync(path.join(root, "rebuild-ready"), "ready");
  } catch (error) { console.error(error); child.kill(); }
});
const child = spawn(electron, [root], { stdio: "inherit", env: { ...process.env, CODING_AGENT_DATA_DIR: root, COSIR_RENDERER_URL: viteUrl } });
child.on("exit", async (code, signal) => { watcher.close(); await vite.close(); if (signal) console.error(`Electron exited on ${signal}`); process.exitCode = code ?? 1; });
