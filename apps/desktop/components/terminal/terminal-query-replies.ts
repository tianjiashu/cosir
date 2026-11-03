import type { IDisposable, Terminal } from "@xterm/xterm";

/**
 * 让面板里的 xterm 不应答程序发来的终端查询：应答只归后端的 Terminal Worker。
 *
 * Worker 的屏幕内核在没有面板时也必须应答（Agent 独占的会话照样有程序查询光标位置），所以它是唯一
 * 应答方。xterm 的应答会经 `onData` 当作用户输入发回后端：面板连着时程序就收到两份应答，多出的一份
 * 落成 `^[[12;1R` 这类乱码，还会被记成「用户在打字」与命令发起方，延误甚至拒绝 Agent 的输入。在解析器
 * 处吞掉查询（处理器返回 true 即不走 xterm 默认应答），不在 `onData` 里按内容过滤，免得误伤用户输入。
 *
 * 范围是 Worker 会答的那批（设备属性、状态与光标位置、模式查询），加上只有 xterm 会答的 DECRQSS 与
 * `CSI ? 6 n`：程序拿到的应答不随面板开没开而变。窗口尺寸查询 `CSI 18 t` 不必处理——xterm 默认不开
 * `windowOptions`，本来就不应答。
 */
export function suppressTerminalQueryReplies(terminal: Terminal): IDisposable[] {
  const swallow = () => true;
  const { parser } = terminal;
  return [
    parser.registerCsiHandler({ final: "c" }, swallow),
    parser.registerCsiHandler({ prefix: ">", final: "c" }, swallow),
    parser.registerCsiHandler({ prefix: "=", final: "c" }, swallow),
    parser.registerCsiHandler({ final: "n" }, swallow),
    parser.registerCsiHandler({ prefix: "?", final: "n" }, swallow),
    parser.registerCsiHandler({ intermediates: "$", final: "p" }, swallow),
    parser.registerCsiHandler({ prefix: "?", intermediates: "$", final: "p" }, swallow),
    parser.registerDcsHandler({ intermediates: "$", final: "q" }, swallow),
  ];
}
