import type {
  TransportMessagePart,
  TransportReasoningPart,
  TransportTextPart,
  TransportToolCallPart,
} from "@/lib/assistant/contract";
import { toolHeading, toolVerb } from "@/lib/assistant/tool-catalog";

/**
 * 回合投影：把一条助手消息的扁平 parts 分成过程 / 等待用户 / 产物 / 结论四区（设计见
 * `docs/plan/conversation-rendering-design.md` §6.1）。纯函数，输入是 Transport part，输出带原始 part
 * 下标，组件用 `MessagePrimitive.PartByIndex` 渲染正文与思考、用 part 本身渲染调用行。
 *
 * 分区规则为什么是精确的而非启发式：模型一次响应里正文先于工具调用，带工具调用的响应不会结束 Run，
 * 所以「最后一个非正文 part 之后的正文」只可能来自不带调用的最后一次响应，即结论。两个边界：流式期间
 * 末尾正文可能随后跟出调用（此时它暂时算结论，回合结束那次才定稿）；运行中途并入用户输入时前一段消息
 * 没有结论、整段都是过程。
 *
 * 过程区包含全部非结论 part：等待用户的调用与产物类调用也留在过程里（§5.4：改动行在过程里也有，与
 * 产物卡互为两种视角），等待区与产物区只是对同一批调用的再投影。
 */

export type TurnPhase = "running" | "suspended" | "settled";

/** 运行中过程区只露最近这么多项，更早的折成「前面还有 N 步」，长回合运行时页面高度有上界。 */
export const ACTIVE_TAIL_SIZE = 4;

/**
 * 产物类 kind 注册：这些展示数据在回合结束后仍要常显，各自成卡放进产物区。
 * `file-changes` 聚合为一张「本轮改动」卡，其余每次调用一张。按 kind 登记而不按工具名，与 renderer 路由同一契约。
 */
export const CHANGES_KIND = "file-changes";
export const STANDALONE_ARTIFACT_KINDS = [
  "agent-configuration-draft",
  "agent-team-configuration-draft",
  "agent-team-approval",
] as const;
export type StandaloneArtifactKind = (typeof STANDALONE_ARTIFACT_KINDS)[number];

export type NarrationItem = { kind: "narration"; index: number; part: TransportTextPart };
export type ReasoningItem = { kind: "reasoning"; index: number; part: TransportReasoningPart };
export type CallItem = { kind: "call"; index: number; part: TransportToolCallPart };
export type ProcessItem = NarrationItem | ReasoningItem | CallItem;

/** 区头计数的键是工具动词（`toolVerb`），不是调用标题：命令的标题是每条各异的意图，按它计数会把「执行命令 4」拆成四个 1。 */
export type VerbCount = { label: string; count: number };

export type ProcessLayout = {
  /** 全部过程项，按原序。 */
  items: ProcessItem[];
  /** 调用次数（思考与叙述不计）。 */
  callCount: number;
  /** 按动词计数，按首次出现排序。 */
  verbCounts: VerbCount[];
  /** 运行中的当前动作：最新未结束调用的标题，没有则「思考中」；非运行态为 null。 */
  currentAction: string | null;
  failedCount: number;
  cancelledCount: number;
  /** 只有 ≤1 次调用时不加区头、直接平铺过程项。 */
  flat: boolean;
  /** 展开时实际渲染的过程项：运行中只保留最近 `ACTIVE_TAIL_SIZE` 项，其余阶段为全部。 */
  tail: ProcessItem[];
  /** 被折进「前面还有 N 步」的项数（只在运行中非零）。 */
  earlierCount: number;
};

export type ChangedFileStatus = "added" | "modified" | "moved" | "deleted";

export type ChangedFile = {
  path: string;
  /** 移动时的目标路径。 */
  newPath: string | null;
  status: ChangedFileStatus;
  additions: number;
  deletions: number;
};

export type ChangesCard = {
  kind: "changes";
  files: ChangedFile[];
  totals: { files: number; additions: number; deletions: number };
  /** 参与聚合的调用 part 下标，供卡片点进某个文件时定位到原 diff。 */
  callIndexes: number[];
};

export type StandaloneArtifactCard = {
  kind: StandaloneArtifactKind;
  index: number;
  part: TransportToolCallPart;
};

export type ArtifactCard = ChangesCard | StandaloneArtifactCard;

export type AttentionItem = { index: number; part: TransportToolCallPart };
export type AnswerItem = { index: number; part: TransportTextPart };

export type TurnLayout = {
  phase: TurnPhase;
  process: ProcessLayout;
  /** `awaitingUser` 为真的调用，按原序。 */
  attention: AttentionItem[];
  artifacts: ArtifactCard[];
  /** 结论：最后一个非正文 part 之后的全部正文，按原序。 */
  answer: AnswerItem[];
};

/** 调用行标题：按工具目录从参数算出，与调用行同源。 */
export function callTitle(part: TransportToolCallPart): string {
  return toolHeading(part.toolName, part.args).title;
}

/** 工具动词：目录里登记的动词，未登记时退回工具名。 */
export function callVerb(part: TransportToolCallPart): string {
  return toolVerb(part.toolName);
}

function isUnfinished(part: TransportToolCallPart): boolean {
  return part.status === "pending" || part.status === "running";
}

function displayKind(part: TransportToolCallPart): string | null {
  const kind = part.display_data?.kind;
  return typeof kind === "string" ? kind : null;
}

function isStandaloneArtifactKind(kind: string | null): kind is StandaloneArtifactKind {
  return (STANDALONE_ARTIFACT_KINDS as readonly string[]).includes(kind ?? "");
}

export function projectTurn(parts: readonly TransportMessagePart[], phase: TurnPhase): TurnLayout {
  // 结论的起点：最后一个非正文 part 的下一个位置；没有非正文 part 时整条消息都是结论。
  let answerStart = 0;
  for (const [index, part] of parts.entries()) {
    if (part.type !== "text") answerStart = index + 1;
  }

  const items: ProcessItem[] = [];
  const answer: AnswerItem[] = [];
  const attention: AttentionItem[] = [];
  const artifacts: ArtifactCard[] = [];
  const changeCalls: Array<{ index: number; part: TransportToolCallPart }> = [];
  const verbCounts = new Map<string, number>();
  let callCount = 0;
  let failedCount = 0;
  let cancelledCount = 0;
  let latestUnfinished: TransportToolCallPart | null = null;

  for (const [index, part] of parts.entries()) {
    if (part.type === "text") {
      if (index >= answerStart) answer.push({ index, part });
      else items.push({ kind: "narration", index, part });
      continue;
    }
    if (part.type === "reasoning") {
      items.push({ kind: "reasoning", index, part });
      continue;
    }
    if (part.type !== "tool-call") continue;
    items.push({ kind: "call", index, part });
    callCount += 1;
    const verb = callVerb(part);
    verbCounts.set(verb, (verbCounts.get(verb) ?? 0) + 1);
    if (part.status === "failed") failedCount += 1;
    else if (part.status === "cancelled") cancelledCount += 1;
    if (isUnfinished(part)) latestUnfinished = part;
    if (part.awaitingUser) attention.push({ index, part });
    const kind = displayKind(part);
    if (kind === CHANGES_KIND) changeCalls.push({ index, part });
    else if (isStandaloneArtifactKind(kind)) artifacts.push({ kind, index, part });
  }

  if (changeCalls.length > 0) {
    // 「本轮改动」排在其他产物卡之前：它是回合里最常见、最需要一眼看到的产物。
    artifacts.unshift(aggregateChanges(changeCalls));
  }

  const running = phase === "running";
  const tail = running ? items.slice(-ACTIVE_TAIL_SIZE) : items;
  return {
    phase,
    process: {
      items,
      callCount,
      verbCounts: [...verbCounts].map(([label, count]) => ({ label, count })),
      currentAction: running ? (latestUnfinished ? callTitle(latestUnfinished) : "思考中") : null,
      failedCount,
      cancelledCount,
      flat: callCount <= 1,
      tail,
      earlierCount: items.length - tail.length,
    },
    attention,
    artifacts,
    answer,
  };
}

/** 同一文件多次变更时取「最终状态」：删除 > 移动 > 新增 > 修改；增删行数累加。 */
const STATUS_RANK: Record<ChangedFileStatus, number> = { modified: 0, added: 1, moved: 2, deleted: 3 };

function readStatus(value: unknown): ChangedFileStatus {
  return value === "added" || value === "moved" || value === "deleted" ? value : "modified";
}

function readCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.trunc(value) : 0;
}

/**
 * 把多次 `file-changes` 调用按文件聚合成一张「本轮改动」卡。按 path 合并；移动的文件以源路径为键，
 * 后续对目标路径的修改按目标路径另起一行（不追踪改名链，改名后再改的情形少见、合并反而难读）。
 */
export function aggregateChanges(calls: ReadonlyArray<{ index: number; part: TransportToolCallPart }>): ChangesCard {
  const files = new Map<string, ChangedFile>();
  for (const { part } of calls) {
    const changes = part.display_data?.changes;
    if (!Array.isArray(changes)) continue;
    for (const raw of changes) {
      if (typeof raw !== "object" || raw === null) continue;
      const change = raw as Record<string, unknown>;
      if (typeof change.path !== "string" || change.path === "") continue;
      const status = readStatus(change.status);
      const existing = files.get(change.path);
      if (existing) {
        existing.additions += readCount(change.insertions);
        existing.deletions += readCount(change.deletions);
        if (STATUS_RANK[status] >= STATUS_RANK[existing.status]) {
          existing.status = status;
          existing.newPath = status === "moved" && typeof change.new_path === "string" ? change.new_path : existing.newPath;
        }
      } else {
        files.set(change.path, {
          path: change.path,
          newPath: status === "moved" && typeof change.new_path === "string" ? change.new_path : null,
          status,
          additions: readCount(change.insertions),
          deletions: readCount(change.deletions),
        });
      }
    }
  }
  const list = [...files.values()];
  return {
    kind: "changes",
    files: list,
    totals: {
      files: list.length,
      additions: list.reduce((sum, file) => sum + file.additions, 0),
      deletions: list.reduce((sum, file) => sum + file.deletions, 0),
    },
    callIndexes: calls.map((call) => call.index),
  };
}
