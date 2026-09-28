import path from "node:path";

/**
 * 返回项目统一使用的 Cargo 构建目录。
 *
 * 桌面宿主、Terminal Worker 和 Tauri 资源 staging 必须共享同一目录，避免
 * 外部环境变量把编译产物与资源目录拆开。该函数不读取或修改文件系统。
 */
export function projectTargetDirectory(repositoryRoot) {
  return path.join(repositoryRoot, "target");
}

/**
 * 构造项目 Cargo 子进程使用的环境变量。
 *
 * 项目明确以仓库根 `target/` 为唯一构建事实源，因此会覆盖调用环境中的
 * `CARGO_TARGET_DIR`。调用方可以通过 extraEnvironment 传递其他环境变量，
 * 但不能改变项目的 Cargo target 目录。
 */
export function projectCargoEnvironment(repositoryRoot, extraEnvironment = {}) {
  return {
    ...process.env,
    ...extraEnvironment,
    CARGO_TARGET_DIR: projectTargetDirectory(repositoryRoot),
  };
}

/**
 * 根据目标 triple 判断 Worker 的平台可执行文件后缀。
 *
 * 交叉构建时必须以目标平台而不是当前 Node 进程所在平台决定文件名；未
 * 提供目标 triple 时才回退到当前宿主平台。该函数不产生文件系统副作用。
 */
export function targetUsesWindowsExecutable(targetTriple = "") {
  return targetTriple ? targetTriple.toLowerCase().includes("windows") : process.platform === "win32";
}
