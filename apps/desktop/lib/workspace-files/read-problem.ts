import { HttpError } from "@/lib/http/http-error";

/**
 * 读取工作区文件失败时属于哪一类（后端错误码见 `api/workspace_files_api.py` 的 `file_errors`）。
 *
 * - `missing-file`：目标不存在（已删除、移动或从未存在）。这是目标的一种状态而不是故障：打开入口据此
 *   不开标签，已打开的标签展示为中性的「文件不存在」，文件恢复时由文件监听自动刷新，不提供「重新读取」。
 * - `missing-workspace`：整个工作区已不可用。
 * - `denied`：后端 403。文件被换成越界链接、没有读权限或系统拒绝访问都会这样，分不开，界面用中性的「无法访问」。
 *   交付物胶囊据此置灰；文件面板仍按读取故障显示（后端文案本身就是中性的）、保留重试。
 * - `error`：其余读取故障（解码失败、读取出错、网络），保留错误样式与重试。
 */
export type FileReadProblem = "missing-file" | "missing-workspace" | "denied" | "error";

export function fileReadProblem(error: unknown): FileReadProblem {
  if (error instanceof HttpError && error.status === 404) {
    if (error.code === "FILE_NOT_FOUND") return "missing-file";
    if (error.code === "WORKSPACE_NOT_FOUND") return "missing-workspace";
  }
  if (error instanceof HttpError && error.status === 403) return "denied";
  return "error";
}
