import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 构建与开发脚本共用的固定路径。
 *
 * 仓库根 `target/` 是唯一的构建目录：前端、Electron 宿主、Terminal Worker 与资源 staging 都落在这里，
 * 不受调用环境里 `CARGO_TARGET_DIR` 的影响（见 `cargo-target.mjs` 的 `projectCargoEnvironment`）。
 */
export const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const repositoryRoot = path.resolve(desktopRoot, "..", "..");
export const targetDirectory = path.join(repositoryRoot, "target");
