import { Unicode11Addon } from "@xterm/addon-unicode11";
import { Terminal } from "@xterm/xterm";

import { reconcileTerminalOutput } from "./reconciler";
import { bufferText, PREVIEW_LINES, SCROLLBACK_ROWS, tailLines, type OutputTail } from "./text";
import { TerminalWriteScheduler } from "./write-scheduler";

/** 解析用的列宽：只决定折行，折行在读出时接回逻辑行，显示时由 CSS 按容器重新换行。 */
const PARSE_COLS = 240;

/**
 * 卡片预览的输出解析器：一个常驻、不挂到页面的 xterm。`\r` 进度帧、光标移动、清行这些控制序列的语义与
 * 阅读器完全一致，再从 buffer 读出带颜色的末尾几行。不进 DOM，所以没有格子排版带来的中文字距、行尾裁切
 * 与光标空行。
 *
 * 输出是累计快照（后端每批送整段当前输出）。与阅读器同一套增量规则（`reconcileTerminalOutput`）：新输出以
 * 已写入的为前缀就只写后缀，否则（输出窗口滑走了开头、终态替换）重置后整段重写；写入经调度器串行，解析
 * 期间到达的多次更新合并成一次。复制用的全文只在需要时从 buffer 读，不随每块输出重算。
 */
export class TerminalOutputParser {
  private readonly terminal: Terminal;
  private readonly scheduler: TerminalWriteScheduler;
  private rendered = "";
  private target = "";

  constructor(private readonly onTail: (tail: OutputTail) => void, onError: (error: unknown) => void) {
    this.terminal = new Terminal({ allowProposedApi: true, convertEol: true, cols: PARSE_COLS, rows: PREVIEW_LINES, scrollback: SCROLLBACK_ROWS });
    this.terminal.loadAddon(new Unicode11Addon());
    this.terminal.unicode.activeVersion = "11";
    this.scheduler = new TerminalWriteScheduler((done) => this.flush(done), onError);
  }

  /** 交给它最新的累计输出；结果经 `onTail` 异步送回，期间的多次更新只解析最后一次。 */
  update(output: string): void {
    this.target = output;
    this.scheduler.request();
  }

  /** 全部已保留输出的纯文本（复制用）；回滚区丢掉的最早部分不在其中。 */
  text(): string {
    return bufferText(this.terminal.buffer.active);
  }

  dispose(): void {
    this.scheduler.dispose();
    this.terminal.dispose();
  }

  private flush(done: () => void): void {
    const target = this.target;
    const action = reconcileTerminalOutput(this.rendered, target);
    if (action.kind === "ignore") {
      done();
      return;
    }
    if (action.kind === "reset") this.terminal.reset();
    this.terminal.write(action.text, () => {
      this.rendered = target;
      this.onTail(tailLines(this.terminal.buffer.active));
      done();
    });
  }
}
