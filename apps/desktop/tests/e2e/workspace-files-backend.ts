import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

import { API_ORIGIN, REAL_BACKEND_ORIGIN, REAL_BACKEND_PORT } from "./e2e-env";

/**
 * 文件面板 E2E 用的真实后端夹具（`apps/backend/tests/fixtures/workspace_files_server.py`）：内存测试服务没有
 * 文件接口，文件请求改写到这里。集成用例与视觉回归共用；同一次运行里只拉起一个，端口随 `COSIR_E2E_SLOT` 错开。
 *
 * 工作区 id 由夹具的 SQLite 自增分配，与内存测试服务的固定 id 无关，`start` 从夹具 health 读出，
 * `route` 改写请求时据它映射。`<img>` 等资源请求同样经 `page.route`，所以直连资源地址也会被改写。
 */
export class WorkspaceFilesBackend {
  private server: ChildProcess | undefined;
  private id: number | undefined;

  get workspaceId(): number {
    if (this.id === undefined) throw new Error("真实后端夹具尚未启动");
    return this.id;
  }

  async start(request: APIRequestContext): Promise<void> {
    const backend = path.resolve(process.cwd(), "../backend");
    const python = path.join(backend, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
    const server = spawn(python, [path.join(backend, "tests/fixtures/workspace_files_server.py"), String(REAL_BACKEND_PORT)], {
      cwd: backend,
      stdio: "pipe",
    });
    this.server = server;
    let output = "";
    server.stderr?.on("data", (chunk) => {
      output += String(chunk);
    });
    await expect
      .poll(
        async () => {
          if (server.exitCode !== null) throw new Error(output);
          try {
            const response = await request.get(`${REAL_BACKEND_ORIGIN}/__test__/files/health`);
            if (!response.ok()) return false;
            this.id = (await response.json()).workspace_id;
            return true;
          } catch {
            return false;
          }
        },
        { timeout: 20_000 },
      )
      .toBe(true);
  }

  /**
   * 先挂退出监听再发信号：进程恰好在检查与挂监听之间退出时，迟挂的监听永远等不到。uvicorn 优雅关闭要等
   * 仍开着的文件监听长连接，宽限 5 秒后强杀，免得收尾钩子超时把整轮判失败。
   */
  async stop(): Promise<void> {
    const server = this.server;
    if (!server) return;
    const exited = new Promise<void>((resolve) => server.once("exit", () => resolve()));
    if (server.exitCode !== null || server.signalCode !== null) return;
    server.kill("SIGTERM");
    const graceful = await Promise.race([exited.then(() => true), new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 5_000))]);
    if (!graceful) {
      server.kill("SIGKILL");
      await exited;
    }
  }

  /** 把前端发往内存测试服务的文件接口改写到夹具，并映射工作区 id。 */
  async route(page: Page): Promise<void> {
    await page.route(/\/workspaces\/\d+\/files\//, (route) =>
      route.continue({
        url: route
          .request()
          .url()
          .replace(new RegExp(`^${API_ORIGIN}/workspaces/\\d+/`), `${REAL_BACKEND_ORIGIN}/workspaces/${this.workspaceId}/`),
      }),
    );
  }
}
