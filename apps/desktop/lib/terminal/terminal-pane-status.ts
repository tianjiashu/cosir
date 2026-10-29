import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import type { TerminalConnectionState } from "@/lib/terminal/terminal-connection";
import { isActiveTerminalSession } from "@/lib/terminal/terminal-sessions";

/**
 * 终端工作台的提示规则（纯函数）。提示分两类，各占一处位置：
 *
 * - 短提示（`TerminalNotice`）：输入被拒、复制失败、新建失败等，几秒后消失，显示在页头右侧。页头
 *   高度固定，短提示出现 / 消失不改变终端行数——若放进面板主体，每次闪现都会触发一次 resize 发往
 *   后端，让 shell 重排画面。
 * - 常驻状态（`TerminalPaneStatus`）：连接中断、失败、会话结束 / 失效，持续到状态改变，显示在页头
 *   下方的状态栏，并就地给出能解决它的操作（重连 / 新建），不让用户去菜单里找。
 *
 * 首次连接（`connecting`）不出常驻状态：它通常一闪而过，状态栏一出一收会让终端行数抖动两次。
 */

export type TerminalNoticeTone = "muted" | "warning" | "destructive";

export type TerminalNotice = { text: string; tone: TerminalNoticeTone };

export type TerminalPaneStatus = TerminalNotice & {
  /** 状态栏上的就地操作：`reconnect` 重连当前会话，`create` 用默认 shell 新建终端。 */
  action: "reconnect" | "create" | null;
};

export function terminalNoticeClass(tone: TerminalNoticeTone): string {
  return tone === "destructive" ? "text-destructive" : tone === "warning" ? "text-warning" : "text-muted-foreground";
}

export function terminalPaneStatus(
  session: TerminalSessionEntry | null,
  connection: TerminalConnectionState | undefined,
): TerminalPaneStatus | null {
  if (session === null) return null;
  switch (connection) {
    case "reconnecting":
      return { text: "连接中断，正在重新连接（画面保留），输入暂停", tone: "warning", action: "reconnect" };
    case "failed":
      return { text: "连接失败，输入已停用", tone: "destructive", action: "reconnect" };
    case "expired":
      // 后端重启后会话自然失效，不是故障，用平静的语气。
      return { text: "该会话已不在后端（可能因后端重启），输入已停用", tone: "muted", action: "create" };
    case "ended": {
      const parts = ["会话已结束，输入已停用"];
      if (session.exitCode !== null) parts.push(`退出码 ${session.exitCode}`);
      if (!isActiveTerminalSession(session)) parts.push("画面受缓冲限制可能不完整");
      return { text: parts.join(" · "), tone: "muted", action: null };
    }
    default:
      return null;
  }
}
