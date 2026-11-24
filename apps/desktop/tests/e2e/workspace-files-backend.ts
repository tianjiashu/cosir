import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import path from "node:path";
import { expect, request as playwrightRequest, type APIRequestContext, type Page } from "@playwright/test";

import { API_ORIGIN, REAL_BACKEND_ORIGIN, REAL_BACKEND_PORT } from "./e2e-env";

/**
 * 文件面板 E2E 用的真实后端夹具（`apps/backend/tests/fixtures/workspace_files_server.py`）：内存测试服务没有
 * 文件接口，文件请求改写到这里。集成用例与视觉回归共用；每个用到它的文件在 beforeAll 各拉起一个，端口按 worker 错开
 * （`e2e-env.ts`），同一 worker 里的文件串行，前一个文件的 afterAll 停掉后下一个才用这个端口。
 *
 * 工作区 id 由夹具的 SQLite 自增分配，与内存测试服务的固定 id 无关，`start` 从夹具 health 读出，
 * `route` 改写请求时据它映射。`<img>` 等资源请求同样经 `page.route`，所以直连资源地址也会被改写。
 *
 * 本机 API 身份（`CODING_AGENT_API_TOKEN` / `x-cosir-capability`，后端 `api/middleware/local_identity.py`）也归这里：
 * 每次 `start` 生成一份，经环境变量交给夹具进程。页面请求在 `route` 改写时带上，对应生产里宿主网络钩子的注入；
 * spec 直连夹具（`__test__` 端点、可见性等）只经 `api`，它以夹具为 baseURL 并已带身份，spec 不拼地址也不碰身份。
 *
 * 就绪只认自己拉起的进程（health 回的 pid 与子进程一致）：端口被遗留的夹具占着时，旧进程照样回应 health，而它的
 * 文件早被上一轮改过，用例会拿到「更新后」的状态、失败得莫名其妙。这里直接报错并给出占用者的 pid。
 */
class PortTakenError extends Error {}

export class WorkspaceFilesBackend {
  private server: ChildProcess | undefined;
  private id: number | undefined;
  private context: APIRequestContext | undefined;
  private readonly identity = { "x-cosir-capability": randomBytes(16).toString("hex") };

  get workspaceId(): number {
    if (this.id === undefined) throw new Error("真实后端夹具尚未启动");
    return this.id;
  }

  /** 直连夹具的请求上下文：路径相对夹具地址（如 `api.post("/__test__/files/update")`）。 */
  get api(): APIRequestContext {
    if (this.context === undefined) throw new Error("真实后端夹具尚未启动");
    return this.context;
  }

  async start(): Promise<void> {
    const request = await playwrightRequest.newContext({ baseURL: REAL_BACKEND_ORIGIN, extraHTTPHeaders: this.identity });
    this.context = request;
    const backend = path.resolve(process.cwd(), "../backend");
    const python = path.join(backend, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
    const server = spawn(python, [path.join(backend, "tests/fixtures/workspace_files_server.py"), String(REAL_BACKEND_PORT)], {
      cwd: backend,
      // stdin 是从不写入的管道：本进程（worker）退出时由系统关闭，夹具读到 EOF 自行退出（`app/parent_watch.py`）。
      env: { ...process.env, CODING_AGENT_API_TOKEN: this.identity["x-cosir-capability"], CODING_AGENT_PARENT_PIPE: "stdin" },
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
            const response = await request.get("/__test__/files/health");
            if (!response.ok()) return false;
            const health = (await response.json()) as { workspace_id: number; pid: number };
            if (health.pid !== server.pid)
              throw new PortTakenError(`端口 ${REAL_BACKEND_PORT} 被另一个夹具进程（pid ${health.pid}）占着，请先结束它`);
            this.id = health.workspace_id;
            return true;
          } catch (error) {
            if (error instanceof PortTakenError) throw error;
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
    await this.context?.dispose();
    this.context = undefined;
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

  /** 把前端发往内存测试服务的文件接口改写到夹具，映射工作区 id 并带上本机身份。 */
  async route(page: Page): Promise<void> {
    await page.route(/\/workspaces\/\d+\/files\//, (route) =>
      route.continue({
        headers: { ...route.request().headers(), ...this.identity },
        url: route
          .request()
          .url()
          .replace(new RegExp(`^${API_ORIGIN}/workspaces/\\d+/`), `${REAL_BACKEND_ORIGIN}/workspaces/${this.workspaceId}/`),
      }),
    );
  }
}
