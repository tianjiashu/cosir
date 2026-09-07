/* global process, fetch, setTimeout */

import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const desktopDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const serverScript = path.join(desktopDirectory, "tests", "e2e", "test-server.mjs");
const healthUrl = "http://127.0.0.1:8000/health";

export default async function globalSetup() {
  const server = spawn(process.execPath, [serverScript], {
    cwd: desktopDirectory,
    stdio: "ignore",
    windowsHide: true,
  });

  try {
    await waitForHealth();
  } catch (error) {
    stopServer(server.pid);
    throw error;
  }

  return async () => {
    stopServer(server.pid);
  };
}

async function waitForHealth() {
  const deadline = Date.now() + 15_000;
  let lastError = "E2E test service did not become ready";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(healthUrl);
      if (response.ok) return;
      lastError = `E2E test service returned HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : lastError;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(lastError);
}

function stopServer(pid) {
  if (!pid) return;
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    } catch {
      // The process may already have exited after a failed setup.
    }
    return;
  }
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    // The process may already have exited after a failed setup.
  }
}
