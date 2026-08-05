/**
 * 瀵硅瘽鐩稿叧璇锋眰 trace 鐘舵€併€?
 *
 * 璁板綍瀹㈡埛绔彂璧风殑浠诲姟銆丼SE銆佸鎵广€佹仮澶嶇瓑瀵硅瘽閾捐矾璇锋眰鎵€浣跨敤鐨?trace_id锛?
 * 渚涙棩蹇楅〉鍜屽彸渚ц瘖鏂潰鏉垮睍绀轰笌鏌ヨ銆?
 *
 * @module stores/conversationTraceStore
 */

import { create } from "zustand";

/**
 * 瀵硅瘽鐩稿叧璇锋眰绫诲瀷銆?
 *
 * 琛ㄧず瀹㈡埛绔湪涓€娆″璇濈敓鍛藉懆鏈熶腑浼氬彂璧风殑 HTTP/SSE 璇锋眰绫诲埆銆?
 * 杩欎簺鍊煎彧鐢ㄤ簬鍓嶇璇婃柇灞曠ず鍜屾棩蹇楁煡璇㈠叆鍙ｉ€夋嫨锛屼笉鍙備笌鍚庣鍗忚鎸佷箙鍖栥€?
 * 绫诲瀷鏈韩娌℃湁杩愯鏃跺壇浣滅敤锛涙湭鐭ョ鐐瑰簲鍏堟墿灞曡鑱斿悎绫诲瀷鍐嶈褰曘€?
 */
export type ConversationTraceOperation =
  | "task_create"
  | "workspace_create"
  | "workspace_tasks"
  | "workspace_delete"
  | "turn_create"
  | "task_turns"
  | "turn_stream"
  | "task_get"
  | "turn_cancel"
  | "task_stream"
  | "task_events"
  | "task_changes"
  | "task_changes_keep"
  | "task_changes_revert"
  | "task_delete"
  | "workspace_index_prepare";

/**
 * 涓€娆″璇濈浉鍏宠姹備娇鐢ㄧ殑 trace 璁板綍銆?
 *
 * 璁板綍鐢?tracePropagation 鐢熸垚骞舵敞鍏ヨ姹傚ご鐨?trace_id锛屼互鍙婂畠鎵€灞炵殑
 * task銆乤pproval銆丠TTP 鏂规硶鍜岃矾寰勩€傝璁板綍鏄墠绔唴瀛樿瘖鏂姸鎬侊紝涓嶄繚璇佸簲鐢?
 * 閲嶅惎鍚庝粛鍙仮澶嶏紱鍚庣鎸佷箙 trace 缁戝畾搴斾綔涓哄崟鐙兘鍔涘疄鐜般€?
 */
export interface ConversationTraceRecord {
  /** 瀹㈡埛绔姹備娇鐢ㄧ殑 trace 鏍囪瘑銆?*/
  traceId: string;
  /** 璇锋眰鎵€灞炰换鍔★紱鍏ㄥ眬璇锋眰鎴栨棤娉曞綊灞炰换鍔℃椂涓虹┖銆?*/
  taskId: string;
  /** 瀵硅瘽璇锋眰绫诲瀷銆?*/
  operation: ConversationTraceOperation;
  /** HTTP 鏂规硶銆?*/
  method: string;
  /** 鍚庣 API 璺緞銆?*/
  path: string;
  /** 鍓嶇璁板綍璇?trace 鐨勬椂闂淬€?*/
  recordedAt: string;
}

/** Trace store 鐘舵€併€?*/
interface ConversationTraceState {
  /** 鎵€鏈夊凡璁板綍鐨勫璇濊姹?trace锛屾寜璁板綍椤哄簭杩藉姞銆?*/
  traces: ConversationTraceRecord[];
  /** 鏈€杩戜竴娆″璇濈浉鍏宠姹?trace銆?*/
  latestTrace: ConversationTraceRecord | null;
  /** 鎸?task_id 绱㈠紩鐨勬渶杩?trace銆?*/
  latestTraceByTaskId: Record<string, ConversationTraceRecord>;
  /** 鎸?task_id 绱㈠紩鐨?SSE stream trace銆?*/
  streamTraceByTaskId: Record<string, ConversationTraceRecord>;
  /** 鎸?task_id 鍜?operation 绱㈠紩鐨勬渶杩?trace銆?*/
  latestTraceByTaskIdAndOperation: Record<string, Partial<Record<ConversationTraceOperation, ConversationTraceRecord>>>;
}

/** Trace store 鍔ㄤ綔銆?*/
interface ConversationTraceActions {
  /** 璁板綍涓€娆″璇濈浉鍏宠姹?trace銆?*/
  recordTrace: (record: Omit<ConversationTraceRecord, "recordedAt">) => void;
  /** 娓呯┖鍏ㄩ儴 trace 鐘舵€侊紝涓昏鐢ㄤ簬娴嬭瘯銆?*/
  resetConversationTraces: () => void;
}

/**
 * 瀵硅瘽 trace Zustand Store銆?
 *
 * 鍙繚瀛樺唴瀛樼姸鎬侊紝涓嶅彂璧?HTTP/SSE/IPC锛屼笉鍐欐棩蹇椼€?
 */
export const useConversationTraceStore = create<ConversationTraceState & ConversationTraceActions>((set) => ({
  traces: [],
  latestTrace: null,
  latestTraceByTaskId: {},
  streamTraceByTaskId: {},
  latestTraceByTaskIdAndOperation: {},

  recordTrace: (record) => {
    const nextRecord: ConversationTraceRecord = {
      ...record,
      recordedAt: new Date().toISOString(),
    };
    set((state) => {
      const latestTraceByTaskId = nextRecord.taskId
        ? { ...state.latestTraceByTaskId, [nextRecord.taskId]: nextRecord }
        : state.latestTraceByTaskId;
      const streamTraceByTaskId =
        nextRecord.taskId && (nextRecord.operation === "task_stream" || nextRecord.operation === "turn_stream")
          ? { ...state.streamTraceByTaskId, [nextRecord.taskId]: nextRecord }
          : state.streamTraceByTaskId;
      const latestTraceByTaskIdAndOperation = nextRecord.taskId
        ? {
            ...state.latestTraceByTaskIdAndOperation,
            [nextRecord.taskId]: {
              ...(state.latestTraceByTaskIdAndOperation[nextRecord.taskId] ?? {}),
              [nextRecord.operation]: nextRecord,
            },
          }
        : state.latestTraceByTaskIdAndOperation;

      return {
        traces: [...state.traces, nextRecord],
        latestTrace: nextRecord,
        latestTraceByTaskId,
        streamTraceByTaskId,
        latestTraceByTaskIdAndOperation,
      };
    });
  },

  resetConversationTraces: () => {
    set({
      traces: [],
      latestTrace: null,
      latestTraceByTaskId: {},
      streamTraceByTaskId: {},
      latestTraceByTaskIdAndOperation: {},
    });
  },
}));

/**
 * 鑾峰彇鎸囧畾浠诲姟鏈€杩戜竴娆″璇濊姹?trace銆?
 *
 * @param taskId - 浠诲姟鏍囪瘑锛岀┖鍊艰繑鍥?null銆?
 * @returns 鍖归厤浠诲姟鐨勬渶杩?trace锛涗笉瀛樺湪鏃惰繑鍥?null銆?
 */
export function getConversationTraceForTask(taskId: string | null): ConversationTraceRecord | null {
  if (!taskId) {
    return null;
  }
  return useConversationTraceStore.getState().latestTraceByTaskId[taskId] ?? null;
}

/**
 * 鑾峰彇鎸囧畾浠诲姟鐨?SSE stream trace銆?
 *
 * @param taskId - 浠诲姟鏍囪瘑锛岀┖鍊艰繑鍥?null銆?
 * @returns 鍖归厤浠诲姟鐨?stream trace锛涗笉瀛樺湪鏃惰繑鍥?null銆?
 */
export function getStreamTraceForTask(taskId: string | null): ConversationTraceRecord | null {
  if (!taskId) {
    return null;
  }
  return useConversationTraceStore.getState().streamTraceByTaskId[taskId] ?? null;
}
