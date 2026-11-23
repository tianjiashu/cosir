import { executingInForeground, segmentActivity, unownedActivity, type ActivityFeedback } from "./current-activity";
import type { BackgroundTaskEntry } from "../contract";
import { callImages, type CallImage } from "../tools/call-images";
import { readFileChanges, type FileChange } from "../tools/file-changes";
import { collectDeliverableBatches, type DeliverablesBatch } from "./deliverable-layout";
import type { TransportActivity, TransportAgentMessagePart, TransportMessagePart, TransportOutcome, TransportTextPart, TransportToolCallPart } from "../contract";
import { readNextStepOffer, type NextStepOffer } from "../tools/next-step-offer";
import { toolHeading, toolVerb } from "../tools/tool-catalog";
import { toolResultSignal } from "../tools/tool-result-signals";

export type TurnPhase = "running" | "suspended" | "settled";
const NO_ARTIFACTS: ArtifactCard[] = [];
/** Only the current canonical reply receives activity; runtime placeholders do not own a Run. */
export function turnPhaseOf(activity: TransportActivity | null): TurnPhase {
  return activity?.status === "running" ? "running" : activity?.status === "suspended" ? "suspended" : "settled";
}
export const CHANGES_KIND = "file-changes";
export const STANDALONE_ARTIFACT_KINDS = ["agent-configuration-draft"] as const;
export const NEXT_STEP_KIND = "next-step";
export type StandaloneArtifactKind = (typeof STANDALONE_ARTIFACT_KINDS)[number];
/**
 * `inProcess`：这段正文已定性为过程（不是最终回复，且 Run 已有结局），随过程开合。Run 运行中、挂起时
 * 正文尚未定性（可能就是最终回复），手动分叉继承的历史没有本地 Run、无从定性，这两种都始终可见、
 * 不构成过程。
 */
export type NarrationItem = { kind: "narration"; index: number; part: TransportTextPart; inProcess: boolean };
/** `live`：独立工具正由前台执行，它那一行自己说状态（活动段因此不说），运行中收起过程也常显。 */
export type StandaloneItem = { kind: "standalone"; index: number; part: TransportToolCallPart; live: boolean };
export type CallItem = { kind: "call"; index: number; part: TransportToolCallPart };
export type AgentItem = { kind: "agent"; index: number; part: TransportAgentMessagePart };
export type ProcessItem = NarrationItem | CallItem | StandaloneItem | AgentItem;
export type VerbCount = { label: string; count: number };
/**
 * 活动段：两次中断（旁白、独立工具、Agent 来信）之间的思考与工具调用，用户眼里的「一段工作」，只占一行。
 * `live` 是这段此刻在做什么（执行中的调用，或回合最后一段上的思考、等待）：运行时这一行说它并计这个状态的
 * 时长（起点见 `ActivityFeedback.since`），段结束原地换回 `summary` 汇总；还没有调用的段（`calls` 为空）只在
 * 运行时存在，出现旁白即被替换。`key` 取上一次中断之后的位置，所以从思考进入第一次调用、中间夹着空白正文或
 * 静默调用，都是同一段、同一行。运行中的段在收起过程时也常显。
 */
export type ActivityGroup = { kind: "activity"; key: string; index: number; calls: CallItem[]; summary: CallSummary; live: ActivityFeedback | null };
/**
 * 交付卡（一次 `present_deliverables` 一张）是回合里的一个块，不属于过程：过程收起时也常显，所以收起后它紧贴在
 * 最终回复上方。它像旁白一样切断活动段——交付是一件有结果的事，前后的调用分成两段。
 */
export type DeliverablesBlock = DeliverablesBatch;
export type TurnBlock = NarrationItem | StandaloneItem | AgentItem | ActivityGroup | DeliverablesBlock;
/**
 * 工具组摘要：按动词计数、失败与取消数，以及这一段的产出——文件改动的增删行数之和（逐次操作相加，不是
 * 工作区净变化）与产出的图片（按出现顺序）。只在组上有，回合过程不另做汇总。问题之外的结果信号（非零退出、
 * 部分成功）仍只留在调用行。
 */
export type CallSummary = {
  verbCounts: VerbCount[];
  failedCount: number;
  cancelledCount: number;
  diff: { insertions: number; deletions: number };
  images: readonly CallImage[];
};
export type FileOperation = { toolCallId: string; change: FileChange };
export type ChangedFile = { path: string; operations: FileOperation[] };
export type ChangesCard = { kind: "changes"; files: ChangedFile[]; totals: { files: number; calls: number }; callIndexes: number[] };
export type StandaloneArtifactCard = { kind: StandaloneArtifactKind; index: number; part: TransportToolCallPart };
export type ArtifactCard = ChangesCard | StandaloneArtifactCard;
export type AnswerItem = { index: number; part: TransportTextPart };
export type TurnLayout = {
  /** 直接引用 canonical parts，供下一版复用未变区间；不反向还原 AUI content。 */
  parts: readonly TransportMessagePart[];
  phase: TurnPhase;
  /** Compact introduction before prose/tools. Derived presentation only, not a model execution phase. */
  intro: "waiting" | null;
  blocks: TurnBlock[];
  /** 过程里的全部条目（标题据此判断能否展开）。 */
  process: { items: ProcessItem[] };
  /**
   * 产出卡（改动、配置草稿）是这一回合的报告，只在落定后出现，与过程默认收起同一刻：它排在回复之后，运行中
   * 出现会被之后的过程一路往下推，挂起时出现又会在唤醒时消失。运行中每次操作的那一行就是反馈。交付卡不在这里，
   * 它是 `blocks` 里的块（见 `DeliverablesBlock`）。
   */
  artifacts: ArtifactCard[];
  /**
   * 这一回合最后一次成功的 `offer_next_step`（同一回合多次调用只认最后一次），展示数据在这里解析一次。这里只说
   * 「有这个建议」；画成可点卡片还是过期痕迹（会话最后一条回复、Run 正常完成才可点）由回合组件按消息位置与结局
   * 判断，不存状态。
   */
  nextStep: { index: number; part: TransportToolCallPart; offer: NextStepOffer } | null;
  answer: AnswerItem[];
  hasFinalReply: boolean;
  defaultProcessOpen: boolean;
};

export function callTitle(part: TransportToolCallPart): string { return toolHeading(part.toolName, part.args).title; }
export function callVerb(part: TransportToolCallPart): string { return toolVerb(part.toolName); }
export function isUnfinished(part: TransportToolCallPart): boolean { return part.status === "pending" || part.status === "running"; }
export function visibleCall(part: TransportToolCallPart): boolean {
  return part.presentation !== "quiet" || part.status === "failed" || part.status === "cancelled"
    || Boolean(part.background) || toolResultSignal(part.display_data) !== null;
}

/**
 * 工具组摘要里的生命周期问题。结果信号（非零退出、部分成功等）只留在调用行：`grep` 无匹配、探测可选
 * 目录都会非零退出，Agent 读到结果后自行处理，汇总上浮只会把常态写成异常；Run 的结局由回合标题表达。
 */
export function describeCallProblems(summary: CallSummary): string | null {
  return [
    summary.failedCount ? `${summary.failedCount} 个失败` : null,
    summary.cancelledCount ? `${summary.cancelledCount} 个取消` : null,
  ].filter(Boolean).join(" · ") || null;
}

/**
 * 工具组头的计数文案。按首次出现排列、只取最早的三类：运行中次数一直在涨，按次数排会让已显示的类别
 * 来回换位；文案因此只往后追加（新类别并入「等 N 类」），不会改动已显示的部分。三类上限让组头保持一行
 * （估高按一行算）。
 */
export function describeVerbCounts(summary: CallSummary): string {
  const shown = summary.verbCounts.slice(0, 3).map(({ label, count }) => `${label} ${count} 次`).join(" · ");
  return shown + (summary.verbCounts.length > 3 ? ` 等 ${summary.verbCounts.length} 类` : "");
}

/** Summaries count calls, not files/steps. Control calls can expose errors without pretending to be work. */
export function summarizeCalls(calls: readonly CallItem[]): CallSummary {
  const verbs = new Map<string, number>();
  let failedCount = 0, cancelledCount = 0, insertions = 0, deletions = 0;
  const images: CallImage[] = [];
  for (const { part } of calls) {
    if (part.presentation !== "quiet") verbs.set(callVerb(part), (verbs.get(callVerb(part)) ?? 0) + 1);
    if (part.status === "failed") failedCount++;
    else if (part.status === "cancelled") cancelledCount++;
    if (part.display_data?.kind === CHANGES_KIND) {
      for (const change of readFileChanges(part.display_data.changes)) { insertions += change.insertions; deletions += change.deletions; }
    }
    images.push(...callImages(part.display_data));
  }
  return { verbCounts: [...verbs].map(([label, count]) => ({ label, count })), failedCount, cancelledCount, diff: { insertions, deletions }, images };
}

/**
 * Stable ordered slots are independent of outcome and subsequent tools; no text ever migrates between parents.
 * 正文是否属于过程取决于 Run 结局（见 `NarrationItem.inProcess`）：结局到达时只改变它随不随过程开合，槽位不动。
 */
export function projectTurn(parts: readonly TransportMessagePart[], phase: TurnPhase, outcome?: TransportOutcome | null, previous?: TurnLayout | null, activity: TransportActivity | null = null, background: readonly BackgroundTaskEntry[] = []): TurnLayout {
  const hasFinalReply = outcome?.status === "completed" && parts.some((part) => part.type === "text" && part.finalReply && part.text.trim());
  const blocks: TurnBlock[] = [], items: ProcessItem[] = [];
  const answer: AnswerItem[] = [], artifacts: ArtifactCard[] = [];
  const changes: CallItem[] = [];
  const previousArtifacts = new Map(previous?.artifacts.flatMap(card => card.kind === "changes" ? [] : [[card.index, card] as const]));
  // 交付卡要先看完整个回合（后来重交的成果从前一张卡里去掉），再在调用的位置插进块序列。
  const batches = new Map(collectDeliverableBatches(parts.flatMap((part, index) => part.type === "tool-call" && part.deliverables?.length ? [{ index, part }] : []))
    .map((batch) => [batch.index, reuseBatch(batch, previous)] as const));
  let group: ActivityGroup | null = null;
  // 当前活动段从哪里开始：上一次中断之后的位置，段的身份取它（见 `ActivityGroup`）。
  let segmentStart = 0;
  let nextStep: TurnLayout["nextStep"] = null;
  for (const [index, part] of parts.entries()) {
    if (part.type === "text") {
      // 纯空白正文不显示，也不切断前后的活动段（模型常在调用前吐一个换行）。
      if (!part.text.trim()) continue;
      group = null;
      segmentStart = index + 1;
      const item: NarrationItem = { kind: "narration", index, part, inProcess: Boolean(outcome) && !part.finalReply };
      blocks.push(item);
      if (!hasFinalReply || part.finalReply) answer.push({ index, part });
      if (item.inProcess) items.push(item);
    } else if (part.type === "tool-call") {
      const batch = batches.get(index);
      if (batch) {
        group = null;
        segmentStart = index + 1;
        blocks.push(batch);
        continue;
      }
      if (part.presentation === "standalone") {
        group = null;
        segmentStart = index + 1;
        const item: StandaloneItem = { kind: "standalone", index, part, live: false };
        blocks.push(item);
        items.push(item);
        continue;
      }
      if (!group) { group = { kind: "activity", key: `interval:${segmentStart}`, index, calls: [], summary: summarizeCalls([]), live: null }; blocks.push(group); }
      const item: CallItem = { kind: "call", index, part };
      if (visibleCall(part)) { group.calls.push(item); items.push(item); }
      const kind = part.display_data?.kind;
      if (kind === CHANGES_KIND) changes.push(item);
      else if (kind === NEXT_STEP_KIND) {
        if (previous?.nextStep?.part === part) nextStep = previous.nextStep;
        else {
          const offer = readNextStepOffer(part.display_data);
          if (offer) nextStep = { index, part, offer };
        }
      }
      else if (STANDALONE_ARTIFACT_KINDS.includes(kind as StandaloneArtifactKind)) {
        const old = previousArtifacts.get(index);
        artifacts.push(old?.part === part ? old : { kind: kind as StandaloneArtifactKind, index, part });
      }
    } else {
      group = null;
      segmentStart = index + 1;
      if (part.type === "agent-message") {
        const item: AgentItem = { kind: "agent", index, part };
        blocks.push(item);
        // 进 `items` 才有可展开的标题：只有来信和最终回复的回合折叠后还要能打开。
        items.push(item);
      }
    }
  }
  // 只有当前回复在运行或挂起时才有「正在做」：继承或历史里未结束的调用不是。
  const board = new Set(background.map(entry => entry.toolCallId));
  const active = phase !== "settled";
  // 独立工具在执行、或有等你操作的卡片时，它们自己那一行说状态，活动段不再说无归属的状态。
  const owned = blocks.some(block => block.kind === "standalone" && (block.part.awaitingUser || active && executingInForeground(block.part, board)));
  // 正文真的在流：末尾是一段输出中的非空正文（不看块，块尾的旁白可能是上一步说完的）。
  const tail = parts.at(-1);
  const proseStreaming = tail?.type === "text" && tail.status === "running" && tail.text.trim() !== "";
  const unowned = active && !owned ? unownedActivity(activity, background, proseStreaming) : null;
  // 无归属的状态只归最后一段；回合末尾不是活动段时（刚说完旁白、独立工具刚结束）补一个还没有调用的段承载它。
  if (unowned && blocks.at(-1)?.kind !== "activity") {
    blocks.push({ kind: "activity", key: `interval:${segmentStart}`, index: parts.length, calls: [], summary: summarizeCalls([]), live: null });
  }
  const last = blocks.at(-1);
  // 只比较 canonical part 身份、槽位与段状态；正文追加不重建活动段、摘要或文件聚合。
  const previousGroups = new Map(previous?.blocks.filter(block => block.kind === "activity").map(block => [block.key, block]));
  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    if (block.kind === "standalone") {
      block.live = active && executingInForeground(block.part, board);
      continue;
    }
    if (block.kind !== "activity") continue;
    block.live = active ? segmentActivity(block.calls.map(call => call.part), board, block === last ? unowned : null) : null;
    const old = previousGroups.get(block.key);
    if (old && old.index === block.index && sameLive(old.live, block.live) && old.calls.length === block.calls.length && old.calls.every((call, j) => call.index === block.calls[j].index && call.part === block.calls[j].part)) {
      blocks[i] = old;
    } else block.summary = summarizeCalls(block.calls);
  }
  hoistDeliverablesAboveReply(blocks);
  if (changes.length) {
    const old = previous?.artifacts.find(card => card.kind === "changes");
    artifacts.unshift(old?.kind === "changes" && sameCalls(old.callIndexes, changes, previous) ? old : aggregateChanges(changes));
  }
  // Any tool (including a hidden wait) ends the first-output introduction. A later silent model
  // request is not observable here, so it must not be inferred from the absence of visible cards.
  const intro = phase === "running" && !outcome && parts.every((part) => part.type === "text" && !part.text.trim())
    ? "waiting" : null;
  const reported = phase !== "settled" ? NO_ARTIFACTS
    : previous?.artifacts.length === artifacts.length && artifacts.every((card, i) => card === previous.artifacts[i]) ? previous.artifacts : artifacts;
  return { parts, phase, intro, blocks, process: { items }, artifacts: reported, nextStep, answer, hasFinalReply,
    defaultProcessOpen: !hasFinalReply };
}

/**
 * 渲染和虚拟估高共用：正在做的事常显（运行中的活动段与独立工具、等你操作的调用），其余过程遵循用户开合——
 * 运行中收起过程不能把唯一说「在做什么」的那一行也藏起来。需要用户决定的工具一律是独立工具（后端
 * `test_tool_registry_display_guard.py` 守着），所以只有独立块要看 `awaitingUser`——收在工具组里的调用会被
 * 折叠的组藏住，不能承载等待。
 */
export function visibleTurnBlock(block: TurnBlock, open: boolean): boolean {
  if (block.kind === "deliverables") return true;
  if (block.kind === "narration") return open || !block.inProcess;
  if (block.kind === "agent") return open;
  if (block.kind === "standalone") return open || block.live || block.part.awaitingUser;
  return block.live !== null || block.calls.length > 0 && open;
}

/**
 * 交付卡排在最终回复之上：模型先交付、再写答案时（提示词要求的顺序）它本来就在那里；把答案和交付写进同一条回复时，
 * 调用排在答案正文之后，落定后（有了最终回复的标记）把它提到第一段最终回复之前，多张保持原顺序。运行中不移动——
 * 那时还不知道哪段是最终回复。只挪块的位置，槽位（`index`）不变。
 */
function hoistDeliverablesAboveReply(blocks: TurnBlock[]): void {
  const reply = blocks.findIndex(block => block.kind === "narration" && block.part.finalReply);
  if (reply === -1) return;
  const late = blocks.filter((block, i) => i > reply && block.kind === "deliverables");
  if (!late.length) return;
  const rest = blocks.filter(block => !late.includes(block));
  rest.splice(rest.indexOf(blocks[reply]), 0, ...late);
  blocks.splice(0, blocks.length, ...rest);
}

/** 交付卡能否沿用上一版：同一槽位、同一 canonical part，且去重后留下的成果不变。 */
function reuseBatch(batch: DeliverablesBatch, previous?: TurnLayout | null): DeliverablesBatch {
  const old = previous?.blocks.find((block): block is DeliverablesBlock => block.kind === "deliverables" && block.index === batch.index);
  return old && old.part === batch.part && old.items.length === batch.items.length && old.items.every((entry, i) => entry.deliverable === batch.items[i].deliverable) ? old : batch;
}

function sameLive(left: ActivityFeedback | null, right: ActivityFeedback | null): boolean {
  return left === right || (left !== null && right !== null && left.label === right.label && left.animated === right.animated && left.since === right.since);
}

/** 改动聚合卡能否复用上一版：参与的调用槽位与 canonical part 身份都没变。 */
function sameCalls(oldIndexes: readonly number[], calls: readonly CallItem[], previous?: TurnLayout | null): boolean {
  return oldIndexes.length === calls.length && calls.every((call, i) => oldIndexes[i] === call.index && previous?.parts[call.index] === call.part);
}

/** Index operation history by path; neither status ranking nor summed diffs describe the final filesystem. */
export function aggregateChanges(calls: ReadonlyArray<{ index: number; part: TransportToolCallPart }>): ChangesCard {
  const files = new Map<string, ChangedFile>();
  for (const { part } of calls) {
    for (const change of readFileChanges(part.display_data?.changes)) {
      const file = files.get(change.path) ?? { path: change.path, operations: [] };
      file.operations.push({ toolCallId: part.toolCallId, change });
      files.set(file.path, file);
    }
  }
  return { kind: "changes", files: [...files.values()], totals: { files: files.size, calls: calls.length }, callIndexes: calls.map((call) => call.index) };
}
