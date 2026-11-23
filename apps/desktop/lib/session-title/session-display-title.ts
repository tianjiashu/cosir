/** 还没起名的会话在界面上的叫法。 */
export const UNTITLED_SESSION_LABEL = "新会话";

/**
 * 会话标题在界面上的显示文本，侧栏、命令面板、面包屑与提示都从这里取。
 *
 * 后端 `sessions.title` 只存真标题，还没起名时为空、不写占位（见后端 `branch/session_title`）；
 * 空标题的叫法只在这一处决定，各界面不各自兜底。
 */
export function sessionDisplayTitle(title: string): string {
  return title.trim() || UNTITLED_SESSION_LABEL;
}
