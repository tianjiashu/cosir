import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const appBinaryPath = path.resolve(desktopRoot, "src-tauri", "target", "debug", "cosir-desktop.exe");

/**
 * Real desktop E2E configuration.
 *
 * This suite starts the compiled Tauri binary. It therefore exercises the
 * Rust host, real WebView2, Tauri IPC and BackendSupervisor; the existing
 * Playwright suite remains a renderer-only reference suite.
 */
export const config = {
  runner: "local",
  specs: ["./tests/tauri-e2e/**/*.spec.mjs"],
  maxInstances: 1,
  services: [["@wdio/tauri-service", {
    appBinaryPath,
    driverProvider: "embedded",
    embeddedPort: 4445,
    captureBackendLogs: true,
    captureFrontendLogs: true,
    env: {
      COSIR_BACKEND_DIR: path.join(repositoryRoot, "apps", "backend"),
    },
    logDir: path.join(desktopRoot, "test-results", "tauri-e2e"),
    logLevel: "info",
    startTimeout: 120000,
    commandTimeout: 60000,
  }]],
  capabilities: [{
    browserName: "tauri",
    "tauri:options": { application: appBinaryPath },
  }],
  logLevel: "info",
  bail: 1,
  waitforTimeout: 15000,
  connectionRetryTimeout: 120000,
  connectionRetryCount: 2,
  framework: "mocha",
  mochaOpts: {
    ui: "bdd",
    timeout: 120000,
  },
  reporters: ["spec"],
};
