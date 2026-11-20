/**
 * 一次 E2E 运行用到的全部地址与输出目录的唯一来源，playwright.config.ts 与各 spec 都从这里取。
 *
 * 每个 worker 独占一套服务（Vite、内存测试服务、真实后端夹具端口）：内存测试服务的状态是进程级全局的，
 * 前端的后端地址又在 Vite 启动时由 `VITE_BACKEND_URL` 定死，所以隔离单位只能是「一个 worker 一套进程」。
 * Playwright 在主进程与每个 worker 里各加载一次配置，worker 加载时 `TEST_PARALLEL_INDEX`（0..workers-1，
 * worker 因失败重启后序号不变）已经设好：worker 里下面的 `*_ORIGIN` 常量就是本 worker 那一套，spec 照常导入；
 * 主进程没有序号，只用 `e2eStack` 列出全部几套交给 webServer 拉起。
 *
 * 多个会话同时跑 E2E 时各设一个不同的 `COSIR_E2E_SLOT`（默认 0），各套端口与输出目录随之错开。端口由
 * slot 与序号确定性推出而不是随机挑选，只有确定性的推导才能让主进程与 worker 看到同一组地址。端口被占时由
 * webServer（`reuseExistingServer: false`）直接报错，不会静默连到别的运行的服务。
 */
const MAX_WORKERS = 8;

const slot = Number(process.env.COSIR_E2E_SLOT ?? 0);
if (!Number.isInteger(slot) || slot < 0) throw new Error(`COSIR_E2E_SLOT 必须是非负整数，当前为 ${process.env.COSIR_E2E_SLOT}`);

const workers = Number(process.env.COSIR_E2E_WORKERS ?? 4);
if (!Number.isInteger(workers) || workers < 1 || workers > MAX_WORKERS)
  throw new Error(`COSIR_E2E_WORKERS 必须是 1 到 ${MAX_WORKERS} 的整数，当前为 ${process.env.COSIR_E2E_WORKERS}`);

export const E2E_SLOT = slot;
/** worker 数：webServer 按它拉起几套服务，`--workers` 不能超过它（超过的 worker 没有自己的服务）。 */
export const E2E_WORKERS = workers;

/** 第 `index` 个 worker 的一套地址；每个 slot 预留 `MAX_WORKERS` 个连续端口，slot 之间不重叠。 */
export function e2eStack(index: number) {
  const offset = slot * MAX_WORKERS + index;
  const webPort = 4173 + offset;
  const apiPort = 8000 + offset;
  const realBackendPort = 8766 + offset;
  return {
    webPort,
    apiPort,
    realBackendPort,
    webOrigin: `http://127.0.0.1:${webPort}`,
    apiOrigin: `http://127.0.0.1:${apiPort}`,
    realBackendOrigin: `http://127.0.0.1:${realBackendPort}`,
  };
}

const parallelIndex = Number(process.env.TEST_PARALLEL_INDEX ?? 0);
if (parallelIndex >= workers)
  throw new Error(`worker 序号 ${parallelIndex} 没有对应的服务：--workers 不能超过 COSIR_E2E_WORKERS（${workers}）`);
const current = e2eStack(parallelIndex);

export const WEB_PORT = current.webPort;
export const API_PORT = current.apiPort;
/** 文件面板用例共用的真实后端夹具（`workspace-files-backend.ts` 拉起，集成用例与视觉回归都用）。 */
export const REAL_BACKEND_PORT = current.realBackendPort;

export const WEB_ORIGIN = current.webOrigin;
/** 内存测试服务 `test-server.mjs`；本 worker 的 Vite 经 `VITE_BACKEND_URL` 指向它。 */
export const API_ORIGIN = current.apiOrigin;
export const REAL_BACKEND_ORIGIN = current.realBackendOrigin;

/** 每个 slot 一个兄弟目录：Playwright 启动时只清空自己的 outputDir，不会清掉别的运行的产物。 */
export const OUTPUT_DIR = `test-results/slot-${slot}`;
