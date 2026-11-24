import {
  SessionTransportConnection,
  type SessionTransportConnectionOptions,
} from "@/lib/assistant/transport/session-transport-connection";
import { frontendLog } from "@/lib/logging/frontend-log";

type Entry = { connection: SessionTransportConnection; viewers: number; unsubscribe: () => void };

/**
 * 会话级持有 Transport 连接：每个（Session，页面类型）至多一个活着的连接。
 *
 * 连接上有会话级的事实——提交顺序链、乐观消息、已收下命令的待上传图片——而页面按 Session 重挂（`SessionPage`
 * 的 key）。连接若随页面创建，切走再回来就换了一条顺序链：上传中的第一条还没发出，回来发的第二条先到后端，
 * 后端按到达顺序决定谁开 Run、谁排队。所以连接活到「没有页面在看，也没有提交在途」为止，两者都没有才回收；
 * 平时切会话（没有在途提交）仍是新连接，内存不随看过的会话增长。
 *
 * 只是进程内协调状态，不跨刷新，不是业务事实。
 */
class SessionTransportConnections {
  private readonly entries = new Map<string, Entry>();

  /** 页面渲染时取连接：活着的就复用，否则新建（还不登记，挂载后经 {@link retain} 登记）。 */
  public connectionFor(options: SessionTransportConnectionOptions): SessionTransportConnection {
    return this.entries.get(SessionTransportConnection.keyOf(options))?.connection ?? new SessionTransportConnection(options);
  }

  /** 页面挂载时调用：登记并开始订阅；返回的函数在卸载时调用。 */
  public retain(connection: SessionTransportConnection): () => void {
    const key = connection.key;
    let entry = this.entries.get(key);
    if (entry && entry.connection !== connection) {
      // 同一 Session 同一种页面同时挂了两个且各自新建了连接：不该发生；第二个只随页面存活，顺序保证只在登记的那个上。
      void frontendLog("ERROR", "assistant_transport_connection_duplicated", "同一 Session 已有活着的连接，新连接不登记", {
        traceId: connection.traceId,
        data: { key },
      });
      connection.start();
      return () => connection.dispose();
    }
    if (!entry) {
      entry = { connection, viewers: 0, unsubscribe: connection.onSubmissionsSettled(() => this.collect(key)) };
      this.entries.set(key, entry);
    }
    entry.viewers += 1;
    if (entry.viewers === 1) connection.start();
    const retained = entry;
    return () => {
      retained.viewers -= 1;
      if (retained.viewers > 0) return;
      connection.stop();
      this.collect(key);
    };
  }

  /** 没有页面也没有在途提交时回收。 */
  private collect(key: string): void {
    const entry = this.entries.get(key);
    if (!entry || entry.viewers > 0 || entry.connection.submitting) return;
    this.entries.delete(key);
    entry.unsubscribe();
    entry.connection.dispose();
  }
}

export const sessionTransportConnections = new SessionTransportConnections();
