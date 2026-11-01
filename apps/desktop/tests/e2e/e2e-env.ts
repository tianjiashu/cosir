/**
 * 一次 E2E 运行用到的全部地址与输出目录的唯一来源，playwright.config.ts 与各 spec 都从这里取。
 *
 * 多个会话同时跑 E2E 时各设一个不同的 `COSIR_E2E_SLOT`（默认 0），Vite、内存测试服务、真实后端夹具
 * 与输出目录随之错开，彼此不共享状态。端口由 slot 确定性推出而不是随机挑选：配置在主进程与每个 worker
 * 里各加载一次，只有确定性的推导才能让两边看到同一组地址。slot 撞上已占用的端口时由 webServer
 * （`reuseExistingServer: false`）直接报错，不会静默连到别的运行的服务。
 */
const slot = Number(process.env.COSIR_E2E_SLOT ?? 0);
if (!Number.isInteger(slot) || slot < 0) throw new Error(`COSIR_E2E_SLOT 必须是非负整数，当前为 ${process.env.COSIR_E2E_SLOT}`);

export const E2E_SLOT = slot;
export const WEB_PORT = 4173 + slot;
export const API_PORT = 8000 + slot;
/** `workspace-files-integration.spec.ts` 拉起的真实后端夹具。 */
export const REAL_BACKEND_PORT = 8766 + slot;

export const WEB_ORIGIN = `http://127.0.0.1:${WEB_PORT}`;
/** 内存测试服务 `test-server.mjs`；前端经 `VITE_BACKEND_URL` 指向它。 */
export const API_ORIGIN = `http://127.0.0.1:${API_PORT}`;
export const REAL_BACKEND_ORIGIN = `http://127.0.0.1:${REAL_BACKEND_PORT}`;

/** 每个 slot 一个兄弟目录：Playwright 启动时只清空自己的 outputDir，不会清掉别的运行的产物。 */
export const OUTPUT_DIR = `test-results/slot-${slot}`;
