/**
 * 宿主有意给用户看的失败：消息是中文、用户能据此理解或纠正（入参不合法、后端正在启动等）。宿主控制面的 IPC 边界
 * （`ipc.ts`）把它的消息原样回给渲染进程；其余异常一律按「宿主操作失败」回复并记日志，不把内部文案透给用户。
 */
export class HostUserError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "HostUserError";
  }
}
