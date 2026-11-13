import { currentActivity, type ActivityFeedback } from "./current-activity";
import type { BackgroundTaskEntry } from "../contract";
import { readFileChanges, type FileChange } from "../tools/file-changes";
import type { TransportActivity, TransportAgentMessagePart, TransportMessagePart, TransportOutcome, TransportTextPart, TransportToolCallPart } from "../contract";
import { toolHeading, toolVerb } from "../tools/tool-catalog";
import { toolResultSignal } from "../tools/tool-result-signals";

export type TurnPhase = "running" | "suspended" | "settled";
/** Only the current canonical reply receives activity; runtime placeholders do not own a Run. */
export function turnPhaseOf(activity: TransportActivity | null): TurnPhase {
  return activity?.status === "running" ? "running" : activity?.status === "suspended" ? "suspended" : "settled";
}
export const CHANGES_KIND = "file-changes";
export const STANDALONE_ARTIFACT_KINDS = ["agent-configuration-draft"] as const;
export type StandaloneArtifactKind = (typeof STANDALONE_ARTIFACT_KINDS)[number];
/**
 * `inProcess`：这段正文已定性为过程（不是最终回复，且 Run 已有结局），随过程开合。Run 运行中、挂起时
 * 正文尚未定性（可能就是最终回复），手动分叉继承的历史没有本地 Run、无从定性，这两种都始终可见、
 * 不构成过程。
 */
export type NarrationItem = { kind: "narration"; index: number; part: TransportTextPart; inProcess: boolean };
export type StandaloneItem = { kind: "standalone"; index: number; part: TransportToolCallPart };
export type CallItem = { kind: "call"; index: number; part: TransportToolCallPart };
export type AgentItem = { kind: "agent"; index: number; part: TransportAgentMessagePart };
export type ProcessItem = NarrationItem | CallItem | StandaloneItem | AgentItem;
export type VerbCount = { label: string; count: number };
export type ActivityGroup = { kind: "activity"; key: string; index: number; calls: CallItem[]; summary: CallSummary };
export type TurnBlock = NarrationItem | StandaloneItem | AgentItem | ActivityGroup;
/** 工具组摘要：按动词计数与失败、取消数。只在组上有，回合过程不另做汇总。 */
export type CallSummary = {
  verbCounts: VerbCount[];
  failedCount: number;
  cancelledCount: number;
};
export type FileOperation = { toolCallId: string; change: FileChange };
export type ChangedFile = { path: string; operations: FileOperation[] };
export type ChangesCard = { kind: "changes"; files: ChangedFile[]; totals: { files: number; calls: number }; callIndexes: number[] };
export type StandaloneArtifactCard = { kind: StandaloneArtifactKind; index: number; part: TransportToolCallPart };
export type ArtifactCard = ChangesCard | StandaloneArtifactCard;
export type AttentionItem = { index: number; part: TransportToolCallPart };
export type AnswerItem = { index: number; part: TransportTextPart };
export type TurnLayout = {
  /** 直接引用 canonical parts，供下一版复用未变区间；不反向还原 AUI content。 */
  parts: readonly TransportMessagePart[];
  phase: TurnPhase;
  current: { groupKey: string; feedback: ActivityFeedback } | null;
  /** Compact introduction before prose/tools. Derived presentation only, not a model execution phase. */
  intro: "waiting" | null;
  blocks: TurnBlock[];
  /** 过程里的全部条目（标题据此判断能否展开）。 */
  process: { items: ProcessItem[] };
  attention: AttentionItem[];
  artifacts: ArtifactCard[];
  answer: AnswerItem[];
  hasFinalReply: boolean;
  defaultProcessOpen: boolean;
};

export function callTitle(part: TransportToolCallPart): string { return toolHeading(part.toolName, part.args, part.intent).title; }
export function callVerb(part: TransportToolCallPart): string { return toolVerb(part.toolName); }
export function isUnfinished(part: TransportToolCallPart): boolean { return part.status === "pending" || part.status === "running"; }
export function visibleCall(part: TransportToolCallPart): boolean {
  return part.presentation !== "quiet" || part.status === "failed" || part.status === "cancelled"
    || part.awaitingUser || Boolean(part.background) || toolResultSignal(part.display_data) !== null;
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

/** Summaries count calls, not files/steps. Control calls can expose errors without pretending to be work. */
export function summarizeCalls(calls: readonly CallItem[]): CallSummary {
  const verbs = new Map<string, number>();
  let failedCount = 0, cancelledCount = 0;
  for (const { part } of calls) {
    if (part.presentation !== "quiet") verbs.set(callVerb(part), (verbs.get(callVerb(part)) ?? 0) + 1);
    if (part.status === "failed") failedCount++;
    else if (part.status === "cancelled") cancelledCount++;
  }
  return { verbCounts: [...verbs].map(([label, count]) => ({ label, count })), failedCount, cancelledCount };
}

/**
 * Stable ordered slots are independent of outcome and subsequent tools; no text ever migrates between parents.
 * 正文是否属于过程取决于 Run 结局（见 `NarrationItem.inProcess`）：结局到达时只改变它随不随过程开合，槽位不动。
 */
export function projectTurn(parts: readonly TransportMessagePart[], phase: TurnPhase, outcome?: TransportOutcome | null, previous?: TurnLayout | null, activity: TransportActivity | null = null, background: readonly BackgroundTaskEntry[] = []): TurnLayout {
  const hasFinalReply = outcome?.status === "completed" && parts.some((part) => part.type === "text" && part.finalReply && part.text.trim());
  const blocks: TurnBlock[] = [], items: ProcessItem[] = [], calls: CallItem[] = [];
  const answer: AnswerItem[] = [], attention: AttentionItem[] = [], artifacts: ArtifactCard[] = [];
  const changes: CallItem[] = [];
  const previousArtifacts = new Map(previous?.artifacts.flatMap(card => card.kind === "changes" ? [] : [[card.index, card] as const]));
  let group: ActivityGroup | null = null;
  for (const [index, part] of parts.entries()) {
    if (part.type === "text") {
      group = null;
      if (!part.text.trim()) continue;
      const item: NarrationItem = { kind: "narration", index, part, inProcess: Boolean(outcome) && !part.finalReply };
      blocks.push(item);
      if (!hasFinalReply || part.finalReply) answer.push({ index, part });
      if (item.inProcess) items.push(item);
    } else if (part.type === "tool-call") {
      if (part.presentation === "standalone") {
        group = null;
        const item: StandaloneItem = { kind: "standalone", index, part };
        blocks.push(item);
        items.push(item);
        calls.push({ kind: "call", index, part });
        continue;
      }
      // Hidden controls still establish the interval identity, so a later error cannot rename this group.
      if (!group) { group = { kind: "activity", key: `interval:${index}`, index, calls: [], summary: summarizeCalls([]) }; blocks.push(group); }
      const item: CallItem = { kind: "call", index, part };
      if (visibleCall(part)) { group.calls.push(item); items.push(item); calls.push(item); }
      if (part.awaitingUser) attention.push({ index, part });
      const kind = part.display_data?.kind;
      if (kind === CHANGES_KIND) changes.push(item);
      else if (STANDALONE_ARTIFACT_KINDS.includes(kind as StandaloneArtifactKind)) {
        const old = previousArtifacts.get(index);
        artifacts.push(old?.part === part ? old : { kind: kind as StandaloneArtifactKind, index, part });
      }
    } else {
      group = null;
      if (part.type === "agent-message") {
        const item: AgentItem = { kind: "agent", index, part };
        blocks.push(item);
        // 进 `items` 才有可展开的标题：只有来信和最终回复的回合折叠后还要能打开。
        items.push(item);
      }
    }
  }
  const feedback = currentActivity(activity, calls.map(call => call.part), background);
  if (feedback && !group) {
    group = { kind: "activity", key: `interval:${parts.length}`, index: parts.length, calls: [], summary: summarizeCalls([]) };
    blocks.push(group);
  }
  const current = feedback && group ? { groupKey: group.key, feedback } : null;
  // 只比较 canonical part 身份与槽位；正文追加不重建工具组、摘要或文件聚合。
  const previousGroups = new Map(previous?.blocks.filter(block => block.kind === "activity").map(block => [block.key, block]));
  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    if (block.kind !== "activity") continue;
    const old = previousGroups.get(block.key);
    if (old && old.index === block.index && old.calls.length === block.calls.length && old.calls.every((call, j) => call.index === block.calls[j].index && call.part === block.calls[j].part)) {
      blocks[i] = old;
    } else block.summary = summarizeCalls(block.calls);
  }
  if (changes.length) {
    const old = previous?.artifacts.find(card => card.kind === "changes");
    const unchanged = old?.kind === "changes" && old.callIndexes.length === changes.length
      && changes.every((call, i) => old.callIndexes[i] === call.index && previous?.parts[call.index] === call.part);
    artifacts.unshift(unchanged ? old : aggregateChanges(changes));
  }
  // Any tool (including a hidden wait) ends the first-output introduction. A later silent model
  // request is not observable here, so it must not be inferred from the absence of visible cards.
  const intro = phase === "running" && !outcome && parts.every((part) => part.type === "text" && !part.text.trim())
    ? "waiting" : null;
  return { parts, phase, current, intro, blocks, process: { items }, attention, artifacts: previous?.artifacts.length === artifacts.length && artifacts.every((card, i) => card === previous.artifacts[i]) ? previous.artifacts : artifacts, answer, hasFinalReply,
    defaultProcessOpen: !hasFinalReply };
}

/** 渲染和虚拟估高共用：需要用户操作的入口常显，其余过程遵循用户开合。 */
export function visibleTurnBlock(block: TurnBlock, open: boolean, currentKey?: string): boolean {
  if (block.kind === "narration") return open || !block.inProcess;
  if (block.kind === "agent") return open;
  if (block.kind === "standalone") return open || block.part.awaitingUser;
  return block.key === currentKey || block.calls.length > 0 && (open || block.calls.some(({ part }) => part.awaitingUser));
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
