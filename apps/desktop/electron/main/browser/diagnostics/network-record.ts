import path from "node:path";

import { appendHostLogLine } from "../../desktop-log";
import { appLogDir } from "../../paths";

/**
 * 内置浏览器网络诊断的唯一落盘点：标签级 CDP 记录与分区级 Cookie 变化写进同一个 `browser-network` 日志，
 * 排查时在一条时间线上对照。沿用宿主日志的按日命名与大小轮转；写入失败由 `appendHostLogLine` 吞掉，不影响宿主。
 */
export function writeNetworkRecord(event: string, data: Record<string, unknown>): void {
  appendHostLogLine(path.join(appLogDir(), "browser-network.log"), {
    level: "INFO", logger: "coding_agent.browser", caller: "browser-diagnostics", event, msg: event, data,
  });
}
