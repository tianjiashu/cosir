/** 被取消的请求记下的失败说明（`read` 照样列出，`digest` 不计）。 */
export const CANCELED_FAILURE = "canceled";

/** 每类条目各留多少：网络请求刷得最快，不能把页面事件与 console 挤掉，所以分开计容量。 */
const CAPACITY = { event: 50, console: 1000, network: 1000 } as const;
/** 单条文本（console 消息、地址）的上限：模型要的是线索，整段压缩代码或 data URL 没有用。 */
const TEXT_MAX_CHARS = 500;

export type ConsoleLevel = "debug" | "info" | "warning" | "error";
const LEVEL_RANK: Record<ConsoleLevel, number> = { debug: 0, info: 1, warning: 2, error: 3 };

/**
 * 响应体的上限分层，只此一处（方案「上限分层」）：
 * - 渲染进程（`Network.enable` 的缓冲，`BrowserTab` 开 Network 时用）决定能不能取到——单条超过的 Chromium 不保留；
 * - 本日志决定存多少——单条超过的截断，每标签总量超过的按先进先出把最早采下的正文挤出（条目保留）。
 * 给模型多少归后端的通用输出预算，不在宿主。
 */
export const BODY_LIMITS = {
  rendererResourceBytes: 1024 * 1024,
  rendererTotalBytes: 8 * 1024 * 1024,
  storedBodyBytes: 256 * 1024,
  storedTotalBytes: 2 * 1024 * 1024,
} as const;

/** 采集响应体的请求类型：只有页面脚本发的请求（接口调用）。文档、脚本、图片等不采。 */
const CAPTURED_TYPES = new Set(["Fetch", "XHR"]);
/** 不会正常结束、没有可采正文的请求类型。 */
const STREAMING_TYPES = new Set(["EventSource"]);

/** 响应体状态（方案「响应体状态表」）：列表行与正文读取共用这一份。 */
export type BodyState = "pending" | "deferred" | "text" | "empty" | "binary" | "too_large" | "evicted" | "streaming" | "unavailable";
/**
 * 条目上的响应体摘要。`size`：text / empty 为解码后正文的 UTF-8 字节数，binary / too_large 为传输大小
 * （`encodedDataLength`），其余为 null。
 */
export type BodySummary = { state: BodyState; mime: string | null; size: number | null };
/** 正文读取的结果：`text` 只在 text / empty 时有；`reason` 说明为什么没有正文（pending / deferred / unavailable 等）。 */
export type ResponseBody = BodySummary & { text: string | null; truncated: boolean; reason: string | null };

export type ConsoleEntry = { seq: number; level: ConsoleLevel; text: string; source: string | null; frame: string | null };
export type NetworkEntry = {
  seq: number;
  method: string;
  url: string;
  status: number | null;
  resourceType: string;
  failure: string | null;
  durationMs: number | null;
  /** 只有 Fetch / XHR（以及标成流式的 EventSource）有；其他类型、重定向的中间跳、失败的请求为 null。 */
  body: BodySummary | null;
};
type EventEntry = { seq: number; text: string };

export type LogQuery =
  | { kind: "console"; since?: number; level?: ConsoleLevel; pattern?: string; limit: number }
  | { kind: "network"; since?: number; failedOnly: boolean; pattern?: string; limit: number };
/**
 * 一页日志与游标说明，三种「没给全」由这里一处判定：
 * - `moreBefore`：省略 since 时，当前页面里更早的匹配条目超出 limit、没有返回；
 * - `moreAfter`：给了 since 时，返回满 limit 之后还有匹配条目（用 `next` 接着读）；
 * - `droppedBefore`：给了 since 时，since 之后有条目已被容量挤掉、再也读不到；值是还在的第一个序号。
 */
export type LogPage<T> = { entries: T[]; next: number; moreBefore: boolean; moreAfter: boolean; droppedBefore: number | null };

/**
 * 页面结果附带的「上次以来」：事件全文，console 与网络只给计数（要看内容用 `browser_logs`）。计数只算当前页面，
 * 与不带 since 的 `read` 同一个范围——提示「去读」时默认读取就能看到；被取消的请求（离开页面时在途的轮询、
 * 页面自己中止的 fetch）不算失败，否则每次导航都会报出读不到、也不是问题的失败。
 */
export type PageDigest = {
  notes: string[];
  newLogs: { consoleErrors: number; consoleWarnings: number; failedRequests: number };
};

/**
 * 一个标签的页面日志：页面事件（拦下的跳转、下载结果、弹窗收尾……，即给 Agent 的备注）、console 与网络请求，
 * 共用一个单调递增、永不复用的序号。「上次以来」的唯一事实源。
 *
 * 读取方各带游标，读取本身不消费任何东西：
 * - 页面结果（navigate / snapshot / act）经 `digest` 取「Agent 上次拿到页面结果以来」的事件与计数，并推进这一个
 *   游标——它是唯一的消费点，所以只为拿标签 id 的调用（如交接）不会吞掉备注；
 * - `browser_logs` 按调用方给的 `since` 读，不动页面结果的游标。
 *
 * 网络条目在请求开始时取序号（`browser_logs` 按它翻页）、之后原地补上状态与耗时；「上次以来」的失败按落定
 * 时刻记：Agent 拿到页面结果时还在途、之后才失败的请求（SPA 首屏的接口常见）计入下一次，不会因为序号早于游标
 * 而漏掉。
 *
 * 响应体是网络条目的附属事实，与条目同生共死：请求结束时由 `BrowserTab` 采下写回（本类不发 CDP），之后导航、
 * 页面停住都照样能读；条目被容量挤掉时正文随之释放。采集被弹窗挡住的条目记为 deferred 并留下 requestId，
 * 弹窗答完后由 `BrowserTab` 取走补采（`takeDeferred`）。
 */
export class TabLog {
  private seq = 0;
  private readonly events: EventEntry[] = [];
  private readonly consoleEntries: ConsoleEntry[] = [];
  private readonly network: NetworkEntry[] = [];
  /** 进行中的请求：CDP requestId → 条目与开始时间（CDP 单调时钟，秒）。 */
  private readonly inFlight = new Map<string, { entry: NetworkEntry; startedAt: number }>();
  /** 当前页面（最近一次主框架跨文档导航）的第一个序号。 */
  private pageStart = 1;
  /**
   * 进行中的主框架导航开始时的下一个序号：主文档请求在提交（`pageStarted`）之前就已记下，要算进新页面。非 null 即
   * 「有一次主框架跨文档导航开始了、还没落定」——标签据此解释加载事件（`navigating`），这件事只在这里记一份。落定只有
   * 两种：换上文档（`pageStarted`），或没换就结束（`navigationAbandoned`：转成下载、204、被拦下）。
   */
  private navigationStart: number | null = null;
  /** 每类缓冲最后被容量挤掉的序号（0 为从没挤掉过，序号从 1 起）：给了 since 的读取据此判断有没有读不到的条目。 */
  private readonly lastDropped = { console: 0, network: 0 };
  /** 页面结果已交付到的序号（不含）：之后的才是「上次以来」。 */
  private delivered = 1;
  /** 交付之前就被容量挤掉的事件数。 */
  private droppedEvents = 0;
  /** 上次交付以来变成失败的请求（按落定时刻，见类说明）；被容量挤掉的随之移除（读不到了）。 */
  private readonly newFailures = new Set<NetworkEntry>();
  /** 已采下的正文，按采下的先后排列（总量超出时从最早的挤出）。 */
  private readonly bodies = new Map<NetworkEntry, { text: string; truncated: boolean; bytes: number }>();
  private storedBytes = 0;
  /** 没有正文时的原因（pending / deferred / unavailable 等）。 */
  private readonly bodyReasons = new Map<NetworkEntry, string>();
  /** 采集被弹窗挡住、等补采的条目 → CDP requestId。 */
  private readonly deferred = new Map<NetworkEntry, string>();

  note(text: string): void {
    this.push(this.events, CAPACITY.event, { seq: this.next(), text }, (dropped) => {
      if (dropped.seq >= this.delivered) this.droppedEvents += 1;
    });
  }

  console(entry: Omit<ConsoleEntry, "seq">): void {
    const clipped = { ...entry, text: clip(entry.text), source: entry.source && clip(entry.source), frame: entry.frame && clip(entry.frame) };
    this.push(this.consoleEntries, CAPACITY.console, { ...clipped, seq: this.next() }, (dropped) => {
      this.lastDropped.console = dropped.seq;
    });
  }

  requestStarted(requestId: string, request: { method: string; url: string; resourceType: string; timestamp: number }): void {
    const entry: NetworkEntry = {
      seq: this.next(),
      method: request.method,
      url: clip(request.url),
      status: null,
      resourceType: request.resourceType,
      failure: null,
      durationMs: null,
      body: STREAMING_TYPES.has(request.resourceType)
        ? { state: "streaming", mime: null, size: null }
        : CAPTURED_TYPES.has(request.resourceType) ? { state: "pending", mime: null, size: null } : null,
    };
    this.push(this.network, CAPACITY.network, entry, (dropped) => {
      this.lastDropped.network = dropped.seq;
      this.newFailures.delete(dropped);
      for (const [id, flight] of this.inFlight) if (flight.entry === dropped) this.inFlight.delete(id);
      this.releaseBody(dropped);
      this.bodyReasons.delete(dropped);
      this.deferred.delete(dropped);
    });
    this.inFlight.set(requestId, { entry, startedAt: request.timestamp });
  }

  /**
   * 拿到响应头（重定向的那一跳也算，`final`：随后同一 requestId 会以新请求的身份再开始，那一跳没有正文）。
   * `mime` 用来在请求结束时决定按文本采还是按二进制只记大小。
   */
  responseReceived(requestId: string, status: number, timestamp: number, final = false, mime: string | null = null): void {
    const flight = this.inFlight.get(requestId);
    if (!flight) return;
    this.update(flight.entry, { status });
    if (final) {
      flight.entry.body = null;
      this.finish(requestId, timestamp);
    } else if (flight.entry.body?.state === "pending") {
      flight.entry.body.mime = mime;
    }
  }

  /**
   * 请求结束。返回要采正文的条目序号（Fetch / XHR 的文本类响应），由调用方发采集再写回；二进制与超过渲染进程
   * 单条缓冲的在这里直接定状态，不必采。`encodedLength` 是 `loadingFinished` 的传输大小。
   */
  requestFinished(requestId: string, timestamp: number, encodedLength: number): number | null {
    const entry = this.inFlight.get(requestId)?.entry;
    this.finish(requestId, timestamp);
    if (!entry?.body || entry.body.state !== "pending") return null;
    if (entry.body.mime !== null && !isTextMime(entry.body.mime)) {
      entry.body = { state: "binary", mime: entry.body.mime, size: encodedLength };
      return null;
    }
    if (encodedLength > BODY_LIMITS.rendererResourceBytes) {
      entry.body = { state: "too_large", mime: entry.body.mime, size: encodedLength };
      return null;
    }
    this.bodyReasons.set(entry, "the body is being captured; read it again shortly");
    return entry.seq;
  }

  requestFailed(requestId: string, errorText: string, timestamp: number): void {
    const flight = this.inFlight.get(requestId);
    if (flight) {
      this.update(flight.entry, { failure: clip(errorText) });
      flight.entry.body = null;
    }
    this.finish(requestId, timestamp);
  }

  /** 采下的正文写回：超过单条上限截断，总量超出时把最早采下的正文挤出。条目已被挤掉时什么也不做。 */
  bodyCaptured(seq: number, text: string): void {
    const entry = this.pendingCapture(seq);
    if (!entry) return;
    const size = Buffer.byteLength(text, "utf8");
    const stored = size > BODY_LIMITS.storedBodyBytes ? utf8Prefix(text, BODY_LIMITS.storedBodyBytes) : text;
    const bytes = Math.min(size, BODY_LIMITS.storedBodyBytes);
    entry.body = { state: size === 0 ? "empty" : "text", mime: entry.body!.mime, size };
    this.bodyReasons.delete(entry);
    this.bodies.set(entry, { text: stored, truncated: size > BODY_LIMITS.storedBodyBytes, bytes });
    this.storedBytes += bytes;
    for (const [oldest] of this.bodies) {
      if (this.storedBytes <= BODY_LIMITS.storedTotalBytes) break;
      this.releaseBody(oldest);
      oldest.body = { ...oldest.body!, state: "evicted" };
      this.bodyReasons.set(oldest, "the body was dropped to make room for newer ones");
    }
  }

  /** 采集时页面停在弹窗：留下 requestId，弹窗答完后补采。 */
  bodyDeferred(seq: number, requestId: string): void {
    const entry = this.pendingCapture(seq);
    if (!entry) return;
    entry.body = { ...entry.body!, state: "deferred" };
    this.bodyReasons.set(entry, "the page was paused by a dialog; the body is captured after the dialog is answered");
    this.deferred.set(entry, requestId);
  }

  bodyUnavailable(seq: number, reason: string): void {
    const entry = this.pendingCapture(seq);
    if (!entry) return;
    entry.body = { ...entry.body!, state: "unavailable" };
    this.bodyReasons.set(entry, reason);
  }

  /** 取走全部等补采的条目（转回 pending，结果照常经 `bodyCaptured` 等写回）。 */
  takeDeferred(): { seq: number; requestId: string }[] {
    const taken = [...this.deferred].map(([entry, requestId]) => {
      entry.body = { ...entry.body!, state: "pending" };
      this.bodyReasons.set(entry, "the body is being captured; read it again shortly");
      return { seq: entry.seq, requestId };
    });
    this.deferred.clear();
    return taken;
  }

  /** 按序号读一条网络请求与它的正文；序号不在网络日志里（没有过、是别的类型、已被挤掉）时为 null。读取不消费任何东西。 */
  readBody(seq: number): { entry: NetworkEntry; body: ResponseBody } | null {
    const entry = this.network.find((item) => item.seq === seq);
    if (!entry) return null;
    const summary = entry.body;
    if (!summary) {
      const reason = entry.failure !== null ? "the request failed, so it has no body" : `bodies are only captured for Fetch and XHR requests, not ${entry.resourceType}`;
      return { entry, body: { state: "unavailable", mime: null, size: null, text: null, truncated: false, reason } };
    }
    const stored = this.bodies.get(entry);
    return {
      entry,
      body: { ...summary, text: stored?.text ?? null, truncated: stored?.truncated ?? false, reason: stored ? null : this.bodyReasons.get(entry) ?? defaultReason(summary.state) },
    };
  }

  get navigating(): boolean {
    return this.navigationStart !== null;
  }

  /** 主框架开始一次跨文档导航（还没提交）：之后记下的主文档请求、重定向属于即将到来的新页面。 */
  navigationStarted(): void {
    this.navigationStart = this.seq + 1;
  }

  /** 那次导航没有换上文档就结束了（转成下载、204、被拦下）：页面还是原来的，之后的条目照旧属于它。没有在途导航时无事。 */
  navigationAbandoned(): void {
    this.navigationStart = null;
  }

  /** 主框架跨文档导航提交：从导航开始起的条目属于新页面（省略 `since` 的日志读取只看当前页面）。 */
  pageStarted(): void {
    this.pageStart = this.navigationStart ?? this.seq + 1;
    this.navigationStart = null;
  }

  /** 页面结果用：取「上次以来」的事件与（当前页面的）计数，并推进交付游标。 */
  digest(): PageDigest {
    const notes = this.events.filter((event) => event.seq >= this.delivered).map((event) => event.text);
    if (this.droppedEvents > 0) notes.unshift(`(${this.droppedEvents} earlier notes were dropped)`);
    const since = Math.max(this.delivered, this.pageStart);
    const newConsole = this.consoleEntries.filter((entry) => entry.seq >= since);
    const newLogs = {
      consoleErrors: newConsole.filter((entry) => entry.level === "error").length,
      consoleWarnings: newConsole.filter((entry) => entry.level === "warning").length,
      failedRequests: [...this.newFailures].filter((entry) => entry.seq >= this.pageStart).length,
    };
    this.delivered = this.seq + 1;
    this.droppedEvents = 0;
    this.newFailures.clear();
    return { notes, newLogs };
  }

  /** 还没交付的事件：标签自己关掉时由 Host 转给接手的标签，不随标签一起消失。 */
  undeliveredNotes(): string[] {
    return this.events.filter((event) => event.seq >= this.delivered).map((event) => event.text);
  }

  read(query: Extract<LogQuery, { kind: "console" }>): LogPage<ConsoleEntry>;
  read(query: Extract<LogQuery, { kind: "network" }>): LogPage<NetworkEntry>;
  read(query: LogQuery): LogPage<ConsoleEntry | NetworkEntry> {
    const pattern = query.pattern?.toLowerCase();
    const matches = query.kind === "console"
      ? this.consoleEntries.filter((entry) => LEVEL_RANK[entry.level] >= LEVEL_RANK[query.level ?? "debug"] && (!pattern || entry.text.toLowerCase().includes(pattern)))
      : this.network.filter((entry) => (!query.failedOnly || isFailed(entry)) && (!pattern || entry.url.toLowerCase().includes(pattern)));
    const end = this.seq + 1;
    if (query.since === undefined) {
      // 当前页面里最新的 limit 条；之后用 next 只读新条目。
      const page = matches.filter((entry) => entry.seq >= this.pageStart);
      return { entries: page.slice(-query.limit), next: end, moreBefore: page.length > query.limit, moreAfter: false, droppedBefore: null };
    }
    const since = query.since;
    const after = matches.filter((entry) => entry.seq >= since);
    const entries = after.slice(0, query.limit);
    const moreAfter = after.length > query.limit;
    const lastDropped = this.lastDropped[query.kind];
    return {
      entries,
      next: moreAfter ? entries.at(-1)!.seq + 1 : end,
      moreBefore: false,
      moreAfter,
      droppedBefore: lastDropped > 0 && since <= lastDropped ? lastDropped + 1 : null,
    };
  }

  private next(): number {
    this.seq += 1;
    return this.seq;
  }

  /** 原地补上请求的结果；只在「首次成为失败」时记入「上次以来」，同一请求不跨页面结果重复计数。 */
  private update(entry: NetworkEntry, change: Partial<Pick<NetworkEntry, "status" | "failure">>): void {
    const failedBefore = isFailed(entry);
    Object.assign(entry, change);
    if (!failedBefore && entry.failure !== CANCELED_FAILURE && isFailed(entry)) this.newFailures.add(entry);
  }

  private finish(requestId: string, timestamp: number): void {
    const flight = this.inFlight.get(requestId);
    if (!flight) return;
    flight.entry.durationMs = Math.max(0, Math.round((timestamp - flight.startedAt) * 1000));
    this.inFlight.delete(requestId);
  }

  /** 还在等采集结果的条目；已被挤掉或不在等（迟到的回执）时为 null。 */
  private pendingCapture(seq: number): NetworkEntry | null {
    const entry = this.network.find((item) => item.seq === seq);
    return entry?.body?.state === "pending" ? entry : null;
  }

  private releaseBody(entry: NetworkEntry): void {
    const stored = this.bodies.get(entry);
    if (!stored) return;
    this.storedBytes -= stored.bytes;
    this.bodies.delete(entry);
  }

  private push<T>(buffer: T[], capacity: number, entry: T, onDrop: (dropped: T) => void): void {
    buffer.push(entry);
    if (buffer.length > capacity) onDrop(buffer.shift()!);
  }
}

function isFailed(entry: NetworkEntry): boolean {
  return entry.failure !== null || (entry.status !== null && entry.status >= 400);
}

/** 按文本采集的 mime（方案「响应体状态表」）；参数（`; charset=…`）不影响判断。 */
function isTextMime(mime: string): boolean {
  const type = mime.split(";")[0].trim().toLowerCase();
  return type.startsWith("text/") || type.endsWith("+json") || type.endsWith("+xml")
    || ["application/json", "application/xml", "application/javascript", "application/x-www-form-urlencoded"].includes(type);
}

/** 取不超过 `maxBytes` 个 UTF-8 字节的前缀，不切坏多字节字符。 */
function utf8Prefix(text: string, maxBytes: number): string {
  const bytes = Buffer.from(text, "utf8");
  let end = maxBytes;
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end -= 1;
  return bytes.subarray(0, end).toString("utf8");
}

function defaultReason(state: BodyState): string | null {
  switch (state) {
    case "binary": return "the response is not text; only its type and size are kept";
    case "too_large": return `the response is larger than the browser keeps (${BODY_LIMITS.rendererResourceBytes} bytes)`;
    case "streaming": return "the response is a stream and has no complete body";
    default: return null;
  }
}

function clip(text: string): string {
  return text.length > TEXT_MAX_CHARS ? `${text.slice(0, TEXT_MAX_CHARS)}…` : text;
}
