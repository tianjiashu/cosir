import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  use: { baseURL: "http://127.0.0.1:4173" },
  webServer: [
    {
      command: ".venv\\Scripts\\python.exe tests/e2e_transport_server.py",
      cwd: "../backend",
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
    },
    {
      command: "npm.cmd run dev -- --host 127.0.0.1 --port 4173",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: true,
    },
  ],
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
