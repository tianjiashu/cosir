import path from "node:path";
import { app } from "electron";

/**
 * 系统级 Cosir 数据根目录：macOS 与 Windows 为用户主目录，其他平台为 Electron 的应用数据目录。
 * 显式设置了 `CODING_AGENT_DATA_DIR`（与直接运行后端时同一个变量）时以它为准，用于桌面 E2E 与开发期
 * 指向隔离的数据目录。
 *
 * 宿主把它作为 `CODING_AGENT_DATA_DIR` 注入后端，由后端统一追加 `.cosir`；宿主自己的日志也写在
 * 同一个 `.cosir/logs` 下，与后端日志并排，排查时只看一个目录。
 */
export function systemDataRoot(): string {
  if (process.env.CODING_AGENT_DATA_DIR) return process.env.CODING_AGENT_DATA_DIR;
  return process.platform === "darwin" || process.platform === "win32"
    ? app.getPath("home")
    : app.getPath("userData");
}

/**
 * 显式指定了数据根时，Electron 自己的 userData（单实例锁、localStorage、会话分区）也放进它下面；须在
 * 申请单实例锁之前调用。否则桌面 E2E 会和用户正在运行的 Cosir 抢同一把单实例锁（测试实例直接退出、
 * 反而唤起用户的窗口），并读写用户真实的界面偏好。
 */
export function isolateUserDataInExplicitDataRoot(): void {
  if (process.env.CODING_AGENT_DATA_DIR) app.setPath("userData", path.join(process.env.CODING_AGENT_DATA_DIR, "electron"));
}

function systemCosirDir(): string {
  return path.join(systemDataRoot(), ".cosir");
}

export function appLogDir(): string {
  return path.join(systemCosirDir(), "logs");
}

/** 宿主与后端共用的运行期文件（启动状态 `backend.bootstate.json`、uv 缓存等），不跨重启保留语义。 */
export function runtimeDir(): string {
  return path.join(systemCosirDir(), "runtime");
}
