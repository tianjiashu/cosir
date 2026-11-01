import type { BrowserTabContext } from "@/electron/shared/host-contract";
import { requestJson } from "@/lib/http/client";

type BrowserContextResponse = {
  limits: { per_task: number; total: number };
  download_dir: string;
};

/**
 * 用户在某个 Task 里新开标签所需的后端事实：两个标签上限（配置中心）与该 Task 所在工作区的下载目录。
 * 后端按 Task 解析，Task 不存在时 404；这里只把 wire 字段换成宿主契约 `BrowserTabContext` 的形状。
 */
export async function getBrowserTabContext(taskId: number): Promise<BrowserTabContext> {
  const { limits, download_dir } = await requestJson<BrowserContextResponse>(`/tasks/${taskId}/browser-context`);
  return { limits: { perTask: limits.per_task, total: limits.total }, downloadDir: download_dir };
}
