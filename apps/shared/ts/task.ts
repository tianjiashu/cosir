/**
 * 浠诲姟鐘舵€佺被鍨嬪畾涔夈€?
 *
 * 涓庡悗绔?`app/storage/records.py` 淇濇寔瀛楁涓€鑷达紝
 * 浣滀负鍓嶅悗绔叡浜殑浠诲姟鐘舵€佸绾︿簨瀹炴簮銆?
 *
 * @module shared/task
 */

/** 浠诲姟鐘舵€佹灇涓撅紝涓庡悗绔?TaskRecord.status 鍙兘鍊煎榻愩€傛敞鎰忥細鍚庣瀹屾垚鎬佷负 "completed" 鑰岄潪 "finished"銆?*/
export type TaskStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

/** 浠诲姟璁板綍鎺ュ彛锛屽搴斿悗绔?`TaskRecord.to_dict()` 杈撳嚭銆?*/
export interface TaskRecord {
  /** 鍞竴鐨勪换鍔℃爣璇嗙锛圲UID锛夈€?*/
  task_id: string;
  /** 鍏宠仈鐨勫伐浣滃尯鏍囪瘑绗︺€?*/
  workspace_id?: string;
  /** 璐熻矗鎵ц浠诲姟鐨?Agent 鏍囪瘑绗︺€?*/
  agent_id: string;
  /** 鍘熷鐨勭函鏂囨湰鐢ㄦ埛浠诲姟杈撳叆銆?*/
  input_text: string;
  /** 浠诲姟瀹瑰櫒鏍囬銆?*/
  title?: string;
  /** 鏈€杩戠敤鎴疯緭鍏ユ憳瑕併€?*/
  last_message_preview?: string;
  /** 鏈€杩戜竴娆¤疆娆℃爣璇嗙銆?*/
  latest_turn_id?: string | null;
  /** 褰撳墠浠诲姟鐘舵€併€?*/
  status: TaskStatus;
  /** 浠诲姟鍒涘缓鏃剁殑 UTC 鏃堕棿鎴筹紙ISO-8601锛夈€?*/
  created_at: string;
  /** 浠诲姟鏈€杩戞洿鏂版椂鐨?UTC 鏃堕棿鎴筹紙ISO-8601锛夈€?*/
  updated_at: string;
}
