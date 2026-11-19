import { clipText } from "./text-clip";

/** 被取消的请求记下的失败说明（`read` 照样列出，`digest` 不计）。 */
export const CANCELED_FAILURE = "canceled";

/** 每类条目各留多少：网络请求刷得最快，不能把页面事件与 console 挤掉，所以分开计容量。 */
const CAPACITY = { event: 50, console: 1000, network: 1000 } as const;
/** 单条文本（console 消息、地址）的上限：模型要的是线索，整段压缩代码或 data URL 没有用。 */
const TEXT_MAX_CHARS = 500;
/** 同一接口的几次发起相隔不到这么久（秒，CDP 时间戳）算同一批：一次动作的扇出，不是周期性的重复。 */
const FAN_OUT_WINDOW_S = 0.05;
/** 接口身份里 GraphQL 操作名的上限：批量请求会把很多名字连在一起。 */
const OPERATION_MAX_CHARS = 120;

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
/** 落定要等的请求类型：页面内容靠它们到达（文档与脚本发的接口调用）。图片、媒体、字体等不影响快照里有没有内容。 */
const DATA_TYPES = new Set(["Document", "XHR", "Fetch"]);
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
 * 一个标签的页面日志：页面事件（拦下的跳转、下载结果、弹窗收尾……，即给 Agent 的备注；对页面状态的落定结论不是事件，
 * 在 `SettleNotes`）、console 与网络请求，共用一个单调递增、永不复用的序号。「上次以来」的唯一事实源。
 *
 * 读取方各带游标，读取本身不消费任何东西：
 * - 页面结果（navigate / snapshot / act）经 `digest` 取「Agent 上次拿到页面结果以来」的事件与计数，并推进这一个
 *   游标——它是唯一的消费点，截图、日志与执行 JS 不会吞掉备注；
 * - `browser_logs` 按调用方给的 `since` 读，不动页面结果的游标。
 *
 * 网络条目在请求开始时取序号（`browser_logs` 按它翻页）、之后原地补上状态与耗时；「上次以来」的失败按落定
 * 时刻记：Agent 拿到页面结果时还在途、之后才失败的请求（SPA 首屏的接口常见）计入下一次，不会因为序号早于游标
 * 而漏掉。
 *
 * 响应体是网络条目的附属事实，与条目同生共死：请求结束时由 `BrowserTab` 采下写回（本类不发 CDP），之后导航、
 * 页面停住都照样能读；条目被容量挤掉时正文随之释放。采集被弹窗挡住的条目记为 deferred，
 * 弹窗答完后由 `BrowserTab` 取走补采（`takeDeferred`）。
 */
export class TabLog {
  private seq = 0;
  private readonly events: EventEntry[] = [];
  private readonly consoleEntries: ConsoleEntry[] = [];
  private readonly network: NetworkEntry[] = [];
  /** 进行中的请求：CDP requestId → 条目（发起时刻等条目元数据在 `requests`）。 */
  private readonly inFlight = new Map<string, NetworkEntry>();
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
  /**
   * 当前页面上页面自己发起过的接口（接口身份见 `endpointOf` → 发起过的 CDP requestId）：没有 Agent 操作、也没有近期用户活动时
   * 页面仍发起它们。发起过两次以上的才算后台流量（埋点、轮询、定时拉取总会重复），落定不再等；只发起过一次的
   * （空闲时懒加载的一次查询）不算，否则一次空闲请求就让这个接口在本页面上永远不等。是不是「页面自己
   * 发起」由标签判定（它持有操作与输入的事实，见 `BrowserTab`），这里只记结论；换文档清空。
   */
  private readonly pageInitiated = new Map<string, Set<string>>();
  /** 条目 → CDP requestId、发起时刻（CDP 时间戳，秒）与接口身份：同一请求的重定向跳共用 requestId，不算重复。 */
  private readonly requests = new Map<NetworkEntry, { id: string; startedAt: number; endpoint: string }>();
  /** 本页面上已经提示过「没等」的接口：同一接口一份文档只提示一次，埋点页面的备注因此有上限；换文档清空。 */
  private readonly unwaitedNoticed = new Set<string>();
  /** 采集被弹窗挡住、等补采的条目（补采用的 requestId 在 `requests`）。 */
  private readonly deferred = new Set<NetworkEntry>();

  /** 下一条记录会拿到的序号：每一步动作开始时记下，落定只等这之后发出的请求（之前就在跑的轮询不算）。 */
  get nextSeq(): number {
    return this.seq + 1;
  }

  /**
   * 落定要等的请求：序号不小于 `from`（这一步开始以来）、还没结束的数据请求（`DATA_TYPES`），且它的接口不是后台流量——
   * 页面自己反复发起过（`pageInitiated`），或这一步里隔开时间重复发起（刚打开的新页面上，埋点还没机会在操作之间被
   * 观察到，靠重复认出）。重复按「发起时刻」分批数：相隔不到 `FAN_OUT_WINDOW_S` 的算同一批，是一次动作的扇出（一次
   * 点击并发请求同一接口，如批量拉取几页），照等；周期性的埋点、轮询在不同时刻反复出现，才认作后台。翻页这类每步一次的请求
   * 因此照等。`unwaited` 是按这两条没等的在途请求，交给落定日志与「没等」的提示（`SettleNotes`）。流式的
   * EventSource 不会结束，不在其中。
   *
   * 已知局限：同一接口（GraphQL 为同一操作）的串行依赖请求（第一条回来后才发第二条）在两个时刻出现，第二条会被当成
   * 重复而不等；这类情况由「没等」的提示交给 Agent。
   */
  pendingRequests(from: number): { waiting: NetworkEntry[]; unwaited: NetworkEntry[] } {
    const starts = new Map<string, Map<string, number>>();
    for (const entry of this.network) {
      if (entry.seq < from || !DATA_TYPES.has(entry.resourceType)) continue;
      const { id, startedAt, endpoint } = this.requests.get(entry)!;
      const ids = starts.get(endpoint) ?? new Map<string, number>();
      ids.set(id, Math.min(ids.get(id) ?? startedAt, startedAt));
      starts.set(endpoint, ids);
    }
    const waiting: NetworkEntry[] = [];
    const unwaited: NetworkEntry[] = [];
    for (const entry of this.inFlight.values()) {
      if (entry.seq < from || !DATA_TYPES.has(entry.resourceType)) continue;
      const { endpoint } = this.requests.get(entry)!;
      const background = (this.pageInitiated.get(endpoint)?.size ?? 0) >= 2 || batches([...(starts.get(endpoint)?.values() ?? [])]) >= 2;
      (background ? unwaited : waiting).push(entry);
    }
    return { waiting, unwaited };
  }

  /** 请求还没结束（被容量挤掉的不算：读不到了）。 */
  running(entry: NetworkEntry): boolean {
    const request = this.requests.get(entry);
    return request !== undefined && this.inFlight.get(request.id) === entry;
  }

  /** 条目的接口身份（`endpointOf`），备注里列出请求时用；条目须还在日志里。 */
  endpoint(entry: NetworkEntry): string {
    return this.requests.get(entry)!.endpoint;
  }

  /** 记下本页面已提示过这个接口「没等」；之前已提示过时返回 false。 */
  noticeUnwaited(endpoint: string): boolean {
    if (this.unwaitedNoticed.has(endpoint)) return false;
    this.unwaitedNoticed.add(endpoint);
    return true;
  }

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

  /** `byPage`：这个请求是页面自己发起的（标签判定），记进 `pageInitiated`。`postData` 只用来认接口身份，不保存。 */
  requestStarted(requestId: string, request: { method: string; url: string; resourceType: string; timestamp: number; postData?: string }, byPage = false): void {
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
      for (const [id, flight] of this.inFlight) if (flight === dropped) this.inFlight.delete(id);
      this.releaseBody(dropped);
      this.bodyReasons.delete(dropped);
      this.deferred.delete(dropped);
      this.requests.delete(dropped);
    });
    this.inFlight.set(requestId, entry);
    const endpoint = endpointOf(request.url, request.postData);
    this.requests.set(entry, { id: requestId, startedAt: request.timestamp, endpoint });
    if (byPage && DATA_TYPES.has(request.resourceType)) {
      this.pageInitiated.set(endpoint, (this.pageInitiated.get(endpoint) ?? new Set<string>()).add(requestId));
    }
  }

  /**
   * 拿到响应头（重定向的那一跳也算，`final`：随后同一 requestId 会以新请求的身份再开始，那一跳没有正文）。
   * `mime` 用来在请求结束时决定按文本采还是按二进制只记大小。
   */
  responseReceived(requestId: string, status: number, timestamp: number, final = false, mime: string | null = null): void {
    const flight = this.inFlight.get(requestId);
    if (!flight) return;
    this.update(flight, { status });
    if (final) {
      flight.body = null;
      this.finish(requestId, timestamp);
    } else if (flight.body?.state === "pending") {
      flight.body.mime = mime;
    }
  }

  /**
   * 请求结束。返回要采正文的条目序号（Fetch / XHR 的文本类响应），由调用方发采集再写回；二进制与超过渲染进程
   * 单条缓冲的在这里直接定状态，不必采。`encodedLength` 是 `loadingFinished` 的传输大小。
   */
  requestFinished(requestId: string, timestamp: number, encodedLength: number): number | null {
    const entry = this.inFlight.get(requestId);
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
      this.update(flight, { failure: clip(errorText) });
      flight.body = null;
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

  /** 采集时页面停在弹窗：记下条目，弹窗答完后补采。 */
  bodyDeferred(seq: number): void {
    const entry = this.pendingCapture(seq);
    if (!entry) return;
    entry.body = { ...entry.body!, state: "deferred" };
    this.bodyReasons.set(entry, "the page was paused by a dialog; the body is captured after the dialog is answered");
    this.deferred.add(entry);
  }

  bodyUnavailable(seq: number, reason: string): void {
    const entry = this.pendingCapture(seq);
    if (!entry) return;
    entry.body = { ...entry.body!, state: "unavailable" };
    this.bodyReasons.set(entry, reason);
  }

  /** 取走全部等补采的条目（转回 pending，结果照常经 `bodyCaptured` 等写回）。 */
  takeDeferred(): { seq: number; requestId: string }[] {
    const taken = [...this.deferred].map((entry) => {
      entry.body = { ...entry.body!, state: "pending" };
      this.bodyReasons.set(entry, "the body is being captured; read it again shortly");
      return { seq: entry.seq, requestId: this.requests.get(entry)!.id };
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
    this.pageInitiated.clear();
    this.unwaitedNoticed.clear();
  }

  /** 页面结果用：取「上次以来」的事件与（当前页面的）计数，并推进交付游标。 */
  digest(): PageDigest {
    const notes = this.undeliveredNotes();
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
    flight.durationMs = Math.max(0, Math.round((timestamp - this.requests.get(flight)!.startedAt) * 1000));
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

/** 截掉的部分不再保存：模型要知道读不到全文是工具的上限。 */
function clip(text: string): string {
  return clipText(text, TEXT_MAX_CHARS);
}

/**
 * 接口的身份：来源 + 路径，GraphQL 请求再加上操作名（`https://x/graphql (Feed)`）。查询参数常带时间戳与签名，同一个
 * 上报接口每次都不同，所以不算在内；GraphQL 的所有操作走同一路径，不加操作名的话一次点击的几条查询、页面自己的
 * 轮询查询与内容查询都混成一个接口。落定判后台流量与备注里列出「哪个接口没回来」用的都是它（查询参数还常带令牌，
 * 不进备注）。
 */
function endpointOf(url: string, postData?: string): string {
  if (!URL.canParse(url)) return url;
  const parsed = new URL(url);
  const operation = graphqlOperation(parsed, postData);
  return `${parsed.origin}${parsed.pathname}${operation === null ? "" : ` (${operation})`}`;
}

/**
 * GraphQL 的操作名：请求体里的 `operationName`（批量请求是数组，取全部），或 GET 持久化查询放在查询参数里的。只认这个
 * 协议约定的字段；请求体不像 GraphQL 就没有操作名。
 */
function graphqlOperation(url: URL, postData: string | undefined): string | null {
  const fromQuery = url.searchParams.get("operationName");
  if (fromQuery) return clipText(fromQuery, OPERATION_MAX_CHARS);
  if (!postData?.includes("operationName")) return null;
  let body: unknown;
  try { body = JSON.parse(postData); } catch { return null; }
  const names = (Array.isArray(body) ? body : [body])
    .map((item: unknown) => (item !== null && typeof item === "object" ? (item as { operationName?: unknown }).operationName : undefined))
    .filter((name): name is string => typeof name === "string" && name !== "");
  return names.length === 0 ? null : clipText(names.join(","), OPERATION_MAX_CHARS);
}

/** 发起时刻分成几批：按时间排好，与上一次相隔超过 `FAN_OUT_WINDOW_S` 就是新的一批。 */
function batches(startedAt: number[]): number {
  return startedAt.sort((a, b) => a - b).filter((time, index, all) => index === 0 || time - all[index - 1] > FAN_OUT_WINDOW_S).length;
}
