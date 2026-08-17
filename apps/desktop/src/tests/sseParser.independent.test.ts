/**
 * createSSEFrameParser 独立补充测试（测试 Agent 视角）。
 *
 * 与开发 Agent 的 sseParser.test.ts（10 例）互补，聚焦开发用例未覆盖的边界：
 * - 单次 feed 内批量多帧的顺序分发
 * - 帧结束分隔符 \n\n 被拆到两次 feed
 * - \r 单独作为行结束（W3C 允许）与 \r\n 混用
 * - data 行前导多空格只剥一个空格（W3C 语义）
 * - event 行前导/尾随空白的工厂 trim 语义（库只剥一个前导空格）
 * - 极端分片：一次 feed 1 个字符
 * - 数千帧压力：无丢帧 / 错序
 * - reset 中间残留半帧后复用不串扰
 * - 首 chunk BOM 剥离（库原生，reset 后再次生效）
 *
 * @module tests/sseParser.independent
 */

import { describe, expect, it } from "vitest";
import { createSSEFrameParser, type ParsedSSEFrame } from "@/services/sseParser";

/**
 * 构造收集分发帧的 parser。
 *
 * @returns parser 与已分发帧数组。
 *
 * @sideeffect 无。
 */
function collect(): { parser: ReturnType<typeof createSSEFrameParser>; frames: ParsedSSEFrame[] } {
  const frames: ParsedSSEFrame[] = [];
  const parser = createSSEFrameParser((frame) => {
    frames.push(frame);
  });
  return { parser, frames };
}

describe("createSSEFrameParser 批量帧与分片边界", () => {
  it("单次 feed 内含多条完整帧：回调按流内顺序逐条触发，不丢帧不错序", () => {
    // 测试目的：验证一次 feed 喂入多帧时逐个 dispatch 且顺序严格等于流内顺序。
    // 可能发现的缺陷：若实现只保留最后一条/首条帧，或回调顺序被反转，会直接暴露。
    const { parser, frames } = collect();
    parser.feed(
      'event: a\ndata: 1\n\n' +
        'event: b\ndata: 2\n\n' +
        'event: c\ndata: 3\n\n',
    );
    expect(frames).toEqual([
      { eventType: "a", data: "1" },
      { eventType: "b", data: "2" },
      { eventType: "c", data: "3" },
    ]);
  });

  it("批量帧中夹带缺 event 名/缺 data 的非法帧：合法帧仍按序分发，非法帧被过滤", () => {
    // 测试目的：批量场景下无 event 名（库会 dispatch 但工厂过滤）与无 data 帧不影响相邻合法帧。
    // 可能发现的缺陷：若过滤逻辑吞掉相邻帧，或把非法帧也分发，会在此暴露。
    const { parser, frames } = collect();
    parser.feed('data: orphan\n\n' + 'event: keep\ndata: 1\n\n' + 'event: onlyEvent\n\n');
    expect(frames).toEqual([{ eventType: "keep", data: "1" }]);
  });

  it("帧与帧之间的 \\n\\n 分隔符被拆到两次 feed（上一段以 \\n 结尾）：仍精确切帧", () => {
    // 测试目的：验证跨 feed 边界的空行分隔符不产生空帧/合并帧。
    // 可能发现的缺陷：若缓冲拼接逻辑把跨边界空行误判为不完整行，会出现丢帧或粘连。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata: 1\n');
    parser.feed('\nevent: b\ndata: 2\n\n');
    expect(frames).toEqual([
      { eventType: "a", data: "1" },
      { eventType: "b", data: "2" },
    ]);
  });

  it("连续两段 feed 各自以 \\n 结尾（分隔符 \\n\\n 正好横跨三次 feed）：仍只切一帧", () => {
    // 测试目的：极端分隔符分片 —— '\n' + '\n' 分属两次 feed 之间。
    // 可能发现的缺陷：若每个 feed 单独处理行结束，可能把单个空行误算成两帧之间的边界而提前 dispatch 空帧。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata: 1\n');
    parser.feed('\n');
    parser.feed('event: b\ndata: 2\n\n');
    expect(frames).toEqual([
      { eventType: "a", data: "1" },
      { eventType: "b", data: "2" },
    ]);
  });

  it("\\r\\n 的 \\r 与 \\n 被拆到两次 feed：仍作为单一终止符处理，不产生空行帧", () => {
    // 测试目的：跨 feed 边界的 CRLF 拆分（库用 skipNextLineFeed 吞掉前导 \n）。
    // 可能发现的缺陷：若把拆开的 \r 和 \n 当作两个终止符，中间会插入一个空行导致提前 dispatch。
    const { parser, frames } = collect();
    parser.feed('event: a\r');
    parser.feed('\ndata: 1\r');
    parser.feed('\n\r');
    parser.feed('\n');
    expect(frames).toEqual([{ eventType: "a", data: "1" }]);
  });
});

describe("createSSEFrameParser 行结束风格", () => {
  it("\\r 单独作为行结束（W3C 允许）：正确解析并 dispatch", () => {
    // 测试目的：验证裸 \r 终止符（旧实现手写 split("\n\n") 不支持，重构需由库兜住）。
    // 可能发现的缺陷：若只支持 \n，裸 \r 流会整条不解析或粘连成一行。
    const { parser, frames } = collect();
    parser.feed('event: run_started\rdata: {"a":1}\r\r');
    expect(frames).toEqual([{ eventType: "run_started", data: '{"a":1}' }]);
  });

  it("\\r 与 \\r\\n 混用（同一流内）：每行仍独立解析，帧边界由空行判定", () => {
    // 测试目的：混合行结束风格（真实后端常 \n 与 \r\n 混发）不串扰。
    // 可能发现的缺陷：若快慢路径切换状态残留，混用流会丢行或错行。
    const { parser, frames } = collect();
    parser.feed('event: a\r\ndata: 1\ndata: 2\r\r');
    expect(frames).toEqual([{ eventType: "a", data: "1\n2" }]);
  });

  it("\\r 单独作为行结束 + 跨 feed 分片：帧仍在完整后 dispatch", () => {
    // 测试目的：裸 \r 流的跨 feed 缓冲拼接。
    // 可能发现的缺陷：若 trailing \r 缓冲逻辑与 fast path 冲突，会丢帧。
    const { parser, frames } = collect();
    parser.feed('event: a\rda');
    parser.feed('ta: 1\r\r');
    expect(frames).toEqual([{ eventType: "a", data: "1" }]);
  });
});

describe("createSSEFrameParser 字段值空白语义", () => {
  it("data: 后一个空格是规范分隔符：data: foo 解析为 foo（无前导空格）", () => {
    // 测试目的：单空格是 'data:' 与值之间的规范分隔符，剥离后不留空格。
    // 可能发现的缺陷：若剥离逻辑错误，data 会带 ' ' 前缀导致 JSON.parse 失败。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata: foo\n\n');
    expect(frames).toEqual([{ eventType: "a", data: "foo" }]);
  });

  it("data: 后接多个空格只剥离一个（W3C）：data:  foo 解析为 ' foo'，前导多余空格保留", () => {
    // 测试目的：W3C 规定仅剥离紧随 'data:' 的一个空格，多余空格属于值的一部分。
    // 可能发现的缺陷：若实现做了完整 trim（旧实现若如此而库不如此，行为会回归），值会被错误裁剪。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata:  foo\n\n');
    expect(frames).toEqual([{ eventType: "a", data: " foo" }]);
  });

  it("data: 后无空格与带空格两种写法都合法：data:foo 与 data: foo 值一致", () => {
    // 测试目的：SSE 规范允许 'data:' 后无空格，两种写法结果相同。
    // 可能发现的缺陷：若只处理带空格写法，无空格帧会被解析成异常值。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata:foo\n\n' + 'event: b\ndata: foo\n\n');
    expect(frames).toEqual([
      { eventType: "a", data: "foo" },
      { eventType: "b", data: "foo" },
    ]);
  });

  it("event 行前导多空格 + 尾随空格：工厂 trim 后 eventType 归一化为纯值", () => {
    // 测试目的：库只剥离一个前导空格且不 trim 尾随，工厂必须补 trim 保持与旧实现一致。
    // 可能发现的缺陷：若工厂漏掉 trim，eventType 会带空格导致事件类型匹配（如 isWorkspaceEventType）失败。
    const { parser, frames } = collect();
    parser.feed('event:   run_started  \ndata: 1\n\n');
    expect(frames).toEqual([{ eventType: "run_started", data: "1" }]);
  });

  it("event 值内部多空格不被 trim 破坏：event: foo bar 保留内部空格", () => {
    // 测试目的：trim 只作用于首尾，值中间的空格必须原样保留。
    // 可能发现的缺陷：若实现用 split(' ') 再 join，内部空格会被折叠。
    const { parser, frames } = collect();
    parser.feed('event: foo bar\ndata: 1\n\n');
    expect(frames).toEqual([{ eventType: "foo bar", data: "1" }]);
  });

  it("data: 后仅一个空格（值即空）：仍视为合法空 data 帧，而非丢弃", () => {
    // 测试目的：'data: '（带空格无内容）与 'data:' 等价，都是合法心跳帧。
    // 可能发现的缺陷：若把 'data: ' 误判为缺 data 行而丢弃，心跳帧会丢。
    const { parser, frames } = collect();
    parser.feed('event: ping\ndata: \n\n');
    expect(frames).toEqual([{ eventType: "ping", data: "" }]);
  });
});

describe("createSSEFrameParser 极端分片与压力", () => {
  it("一次 feed 只喂 1 个字符：多帧长流最终仍完整、按序解析（含 \\r\\n 终止符）", () => {
    // 测试目的：逐字符喂入是最极端的分片，验证缓冲拼接无上限边界缺陷。
    // 可能发现的缺陷：若缓冲实现按 feed 边界截断，1 字符喂入会丢帧/乱序/截断。
    const { parser, frames } = collect();
    const stream = 'event: a\ndata: {"x":1}\r\ndata: tail\n\r\n' + 'event: b\ndata: 2\n\n';
    for (const ch of stream) {
      parser.feed(ch);
    }
    expect(frames).toEqual([
      { eventType: "a", data: '{"x":1}\ntail' },
      { eventType: "b", data: "2" },
    ]);
  });

  it("大量帧压力：单次 feed 2000 帧全部分发，无丢帧、错序、串扰", () => {
    // 测试目的：验证高吞吐下回调次数与顺序严格正确（防丢帧/错序/内容污染）。
    // 可能发现的缺陷：若内部状态复用（eventType/data 未及时重置），后续帧会带前帧残留。
    const { parser, frames } = collect();
    let payload = "";
    for (let i = 0; i < 2000; i += 1) {
      payload += `event: evt${i}\ndata: ${i}\n\n`;
    }
    parser.feed(payload);
    expect(frames).toHaveLength(2000);
    for (let i = 0; i < 2000; i += 1) {
      expect(frames[i]).toEqual({ eventType: `evt${i}`, data: `${i}` });
    }
  });

  it("大量帧跨多次小分片 feed（每次 64 字符）：仍无丢帧、不错序", () => {
    // 测试目的：压力 + 分片组合，覆盖缓冲路径而非单次 fast path。
    // 可能发现的缺陷：缓冲拼接与帧边界处理组合出错时仅在小分片场景暴露。
    const { parser, frames } = collect();
    let payload = "";
    for (let i = 0; i < 500; i += 1) {
      payload += `event: e${i}\ndata: ${i}\n\n`;
    }
    for (let i = 0; i < payload.length; i += 64) {
      parser.feed(payload.slice(i, i + 64));
    }
    expect(frames).toHaveLength(500);
    for (let i = 0; i < 500; i += 1) {
      expect(frames[i]).toEqual({ eventType: `e${i}`, data: `${i}` });
    }
  });
});

describe("createSSEFrameParser reset 语义", () => {
  it("残留半帧时 reset：旧流不完整残片不消费，新流从干净状态开始", () => {
    // 测试目的：reset 应清空所有内部状态（含 pendingFragments / eventType / data），
    //   与 sse.ts 的 EOF feed("\n\n")+reset 约定一致。
    // 可能发现的缺陷：若 reset 不清 pendingFragments，旧残片会拼进新流首帧造成内容污染。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata: partial'); // 半帧残留
    parser.reset();
    parser.feed('event: b\ndata: fresh\n\n');
    expect(frames).toEqual([{ eventType: "b", data: "fresh" }]);
  });

  it("已完整帧（已 dispatch）后 reset 再复用：旧帧不重复、新帧正常", () => {
    // 测试目的：reset 不消费不完整残片，但已完整分发的帧不应在复用后重复出现。
    // 可能发现的缺陷：若 reset 后库把缓冲残片当完整帧补发，会多出脏帧。
    const { parser, frames } = collect();
    parser.feed('event: a\ndata: 1\n\n');
    parser.reset();
    parser.feed('event: b\ndata: 2\n\n');
    expect(frames).toEqual([
      { eventType: "a", data: "1" },
      { eventType: "b", data: "2" },
    ]);
  });

  it("reset 后新流首 chunk 以 BOM 开头：BOM 被剥离（库首 chunk BOM 处理，reset 后再次生效）", () => {
    // 测试目的：库在每段流的首 chunk 剥离 UTF-8 BOM；reset 复用同一实例时该行为应再次生效。
    // 可能发现的缺陷：若 BOM 残留进 event 名或 data，事件类型匹配/JSON.parse 都会失败。
    const { parser, frames } = collect();
    parser.feed("\ufeffevent: a\ndata: 1\n\n");
    parser.reset();
    parser.feed("\ufeffevent: b\ndata: 2\n\n");
    expect(frames).toEqual([
      { eventType: "a", data: "1" },
      { eventType: "b", data: "2" },
    ]);
  });

  it("空字符串 feed 与空帧文本 feed 是幂等空操作，不产生脏帧", () => {
    // 测试目的：空 chunk 与空文本（\n\n 但无内容）不得产生任何 dispatch。
    // 可能发现的缺陷：若空 chunk 触发缓冲解析，可能误产出空帧。
    const { parser, frames } = collect();
    parser.feed("");
    parser.feed("\n\n");
    expect(frames).toEqual([]);
    // 空操作后正常帧仍工作
    parser.feed('event: a\ndata: 1\n\n');
    expect(frames).toEqual([{ eventType: "a", data: "1" }]);
  });
});
