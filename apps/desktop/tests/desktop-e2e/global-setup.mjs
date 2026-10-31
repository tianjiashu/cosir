import { buildDevelopmentTerminalWorker } from "../../scripts/cargo-target.mjs";

/** 桌面 E2E 驱动真实宿主，宿主要拉起真实后端，后端需要 Terminal Worker：先构建它并交给用例。 */
export default async function globalSetup() {
  process.env.CODING_AGENT_TERMINAL_WORKER = buildDevelopmentTerminalWorker();
}
