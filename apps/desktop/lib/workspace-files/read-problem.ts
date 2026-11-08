import { HttpError } from "@/lib/http/http-error";

/**
 * 读取工作区文件失败时属于哪一类（后端错误码见 `api/workspace_files_api.py` 的 `file_errors`）。
 *
 * - `missing-file`：目标不存在（已删除、移动或从未存在）。这是目标的一种状态而不是故障：打开入口据此
 *   不开标签，已打开的标签展示为中性的「文件不存在」，文件恢复时由文件监听自动刷新，不提供「重新读取」。
 * - `missing-workspace`：整个工作区已不可用。
 * - `error`：真正的读取故障（无权限、解码失败、读取出错、网络），保留错误样式与重试。
 */
export type FileReadProblem = "missing-file" | "missing-workspace" | "error";

export function fileReadProblem(error: unknown): FileReadProblem {
  if (error instanceof HttpError && error.status === 404) {
    if (error.code === "FILE_NOT_FOUND") return "missing-file";
    if (error.code === "WORKSPACE_NOT_FOUND") return "missing-workspace";
  }
  return "error";
}
