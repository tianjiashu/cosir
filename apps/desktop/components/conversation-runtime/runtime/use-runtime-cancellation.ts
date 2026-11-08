import type { RuntimeSessionContext } from "./runtime-types";
import { useCommandRequest } from "@/lib/assistant/command-request-store";

/** 这个 Task 的当前活动是否正在停止：请求身份在 store，何时算结束以 canonical 活动为准。 */
export function useRuntimeCancellation(context: RuntimeSessionContext): boolean {
  const request = useCommandRequest(context.taskId, "task");
  const activity = context.latestStateRef.current.activity;
  return request !== undefined && !request.error && (activity?.status === "running" || activity?.status === "suspended");
}
