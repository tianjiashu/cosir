import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import {
  SessionTransportConnection,
  type SessionTransportConnectionOptions,
  type TransportConnectionSnapshot,
} from "@/lib/assistant/transport/session-transport-connection";
import { sessionTransportConnections } from "@/lib/assistant/transport/session-transport-connections";

/**
 * 外壳（主对话页、Workbench 阅读页）使用一个 Session 的连接：连接归会话级登记表（还有提交在途时，切走再回来
 * 拿到的是同一个，见 `session-transport-connections`），外壳挂载期间登记、卸载时释放，并订阅它的快照（生命周期
 * 与连接问题）。连接选项只在新建时读取：复用的连接沿用它自己的身份，链路 id 以 `connection.traceId` 为准。
 */
export function useSessionTransportConnection(
  options: SessionTransportConnectionOptions,
): { connection: SessionTransportConnection } & TransportConnectionSnapshot {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const { sessionId } = options;
  // 连接身份只随 Session 变化；其余选项取新建时的值（见上）。
  const connection = useMemo(() => sessionTransportConnections.connectionFor(optionsRef.current), [sessionId]);
  useEffect(() => sessionTransportConnections.retain(connection), [connection]);
  const snapshot = useSyncExternalStore(connection.subscribe, connection.getSnapshot, connection.getSnapshot);
  return { connection, ...snapshot };
}
