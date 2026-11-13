import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import {
  TaskTransportConnection,
  type TaskTransportConnectionOptions,
  type TransportConnectionSnapshot,
} from "@/lib/assistant/transport/task-transport-connection";

/**
 * 外壳（主对话页、Workbench 阅读页）持有一个 Task 的连接：按 `taskId` 创建，挂载期间 start、卸载时 stop，
 * 并订阅它的快照（生命周期与连接问题）。连接选项只在创建时读取一次：同一个连接的身份（Task、链路 id、
 * 页面镜像同步）在它的生命周期里不变。
 */
export function useTaskTransportConnection(
  options: TaskTransportConnectionOptions,
): { connection: TaskTransportConnection } & TransportConnectionSnapshot {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const { taskId } = options;
  // 连接身份只随 Task 变化；其余选项取创建时的值（见上）。
  const connection = useMemo(() => new TaskTransportConnection(optionsRef.current), [taskId]);
  useEffect(() => {
    connection.start();
    return () => connection.stop();
  }, [connection]);
  const snapshot = useSyncExternalStore(connection.subscribe, connection.getSnapshot, connection.getSnapshot);
  return { connection, ...snapshot };
}
