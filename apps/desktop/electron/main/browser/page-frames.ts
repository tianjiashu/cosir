import { logDesktopEvent } from "../desktop-log";
import type { CdpSession } from "./cdp-session";
import { browserError } from "./errors";

/** 主会话与每个跨进程子框架的子会话都要设：嵌套的跨进程子框架才会继续附加上来。 */
const AUTO_ATTACH = { autoAttach: true, waitForDebuggerOnStart: false, flatten: true };

/** 一份文档在哪：所在会话（主会话为 undefined），同进程的子框架另给 frameId（会话自己的主框架不给）。 */
export type FrameDocument = { sessionId?: string; frameId?: string };

/** 跨进程子框架的子会话，以及它的 iframe 元素所在的会话（上一层）。 */
type RemoteFrame = { frameId: string; sessionId: string; parentSessionId: string | undefined };

/**
 * 一个标签页里的 iframe 在 CDP 上的位置（实测）：
 * - 同进程的子框架与上一层共用会话：节点 id 是同一套，盒模型坐标已是主视口坐标，按 frameId 取它的无障碍树。
 * - 跨进程的（跨站嵌入）经自动附加得到子会话：节点 id 与上一层重号，坐标是框架内的，要加上它的 iframe 元素在
 *   上一层的位置（`offset`）。合成输入仍发主会话（视图只有一个），聚焦等按节点的命令发子会话。
 * iframe 元素经 `DOM.describeNode` 得到它装的 frameId，跨进程子框架的 frameId 就是子会话的 targetId。
 *
 * 子会话附加时开自动附加与选文件框拦截（与主会话同一规则：Agent 动作打开的交给 `upload`，否则会弹出系统选择框），
 * 不开 Network：网络日志只看主会话。子框架导航换进程或被移除时子会话分离，这里随之忘掉它；指向它的 ref 由命令
 * 失败报 `REF_STALE`。
 */
export class PageFrames {
  /** 跨进程子框架，按子会话 id。 */
  private readonly remote = new Map<string, RemoteFrame>();

  constructor(private readonly session: CdpSession, private readonly tabId: string) {}

  start(signal: AbortSignal): Promise<unknown> {
    return this.session.send("Target.setAutoAttach", AUTO_ATTACH, { signal });
  }

  /** `Target.attachedToTarget`：`parentSessionId` 是事件来源会话，即 iframe 元素所在的那一层。 */
  attached({ sessionId, targetInfo }: { sessionId: string; targetInfo: { targetId: string; type: string } }, parentSessionId: string | undefined): void {
    if (targetInfo.type !== "iframe") return;
    this.remote.set(sessionId, { frameId: targetInfo.targetId, sessionId, parentSessionId });
    const send = (method: string, params: Record<string, unknown>) => this.session.send(method, params, { sessionId });
    Promise.all([send("Target.setAutoAttach", AUTO_ATTACH), send("Page.enable", {}).then(() => send("Page.setInterceptFileChooserDialog", { enabled: true }))]).catch((error: unknown) => {
      logDesktopEvent("WARNING", "browser", "browser_frame_attach_failed", { tab_id: this.tabId, error: error instanceof Error ? error.message : String(error) });
    });
  }

  detached({ sessionId }: { sessionId: string }): void {
    this.remote.delete(sessionId);
  }

  /** iframe 元素（`owner`，在会话 `at` 的文档里）装的文档；还没装上文档时为 null。 */
  async contentOf(at: string | undefined, owner: number): Promise<FrameDocument | null> {
    const { node } = await this.session.send<{ node: { frameId?: string } }>("DOM.describeNode", { backendNodeId: owner }, { sessionId: at });
    if (!node.frameId) return null;
    const remote = [...this.remote.values()].find((frame) => frame.frameId === node.frameId);
    return remote ? { sessionId: remote.sessionId } : { sessionId: at, frameId: node.frameId };
  }

  /**
   * 会话里的视口坐标 → 主视口坐标的偏移：逐层加上 iframe 元素内容框的左上角（主会话为 0）。先由内向外把每层 iframe
   * 元素滚入视野、再统一量：滚动会传到外层框架，边滚边量的话，先量的外层位置会被后面的滚动作废。
   */
  async offset(sessionId: string | undefined): Promise<{ x: number; y: number }> {
    const owners: { sessionId: string | undefined; backendNodeId: number }[] = [];
    for (let at = sessionId; at !== undefined;) {
      const frame = this.remote.get(at);
      if (!frame) throw browserError("REF_STALE", "the frame holding the element is gone; take a new snapshot");
      const { backendNodeId } = await this.session.send<{ backendNodeId: number }>("DOM.getFrameOwner", { frameId: frame.frameId }, { sessionId: frame.parentSessionId });
      owners.push({ sessionId: frame.parentSessionId, backendNodeId });
      at = frame.parentSessionId;
    }
    for (const owner of owners) await this.session.send("DOM.scrollIntoViewIfNeeded", { backendNodeId: owner.backendNodeId }, { sessionId: owner.sessionId });
    const offset = { x: 0, y: 0 };
    for (const owner of owners) {
      const { model } = await this.session.send<{ model: { content: number[] } }>("DOM.getBoxModel", { backendNodeId: owner.backendNodeId }, { sessionId: owner.sessionId });
      offset.x += model.content[0];
      offset.y += model.content[1];
    }
    return offset;
  }
}
