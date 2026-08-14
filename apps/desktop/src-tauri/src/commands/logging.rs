//! 前端日志落盘命令。

use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;

/// 单个日志文件达到该字节数即触发分片（5 MB）。
const MAX_LOG_SIZE: u64 = 5 * 1024 * 1024;

/// 分片后保留的历史文件份数（不含当前 `desktop.log`）。
const MAX_BACKUPS: u32 = 5;

/// 日志条目结构（从前端传入）。
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    /// 日志级别 (debug/info/warn/error)。
    pub level: String,
    /// 日志消息文本。
    pub message: String,
    /// ISO-8601 时间戳。
    pub timestamp: String,
    /// 可选的附加上下文 JSON 字符串。
    #[serde(default)]
    pub context: Option<serde_json::Value>,
    /// 可选的错误堆栈。
    #[serde(default)]
    pub stack: Option<String>,
}

/// 将前端日志条目追加写入 `desktop.log`。
///
/// 写入前若当前文件大小将超过 `MAX_LOG_SIZE`，先执行分片轮转：
/// `desktop.log` → `desktop.log.1` → … → `desktop.log.MAX_BACKUPS`（最旧者删除）。
/// 日志目录统一为仓库根 `logs/`（与后端日志目录约定一致，便于开发期排查）。
///
/// 参数:
///     app: Tauri 应用句柄，用于解析平台日志目录。
///     entry: 由前端传入的结构化日志条目。
///
/// 返回:
///     成功时返回 `Ok(())`。
///
/// 异常:
///     当日志目录解析、文件打开或写入失败时，返回错误字符串。
///
/// 副作用:
///     创建日志目录并向 `desktop.log` 追加写入一条或多条日志；
///     超阈值时触发历史分片轮转。
#[tauri::command]
pub fn log_write(app: tauri::AppHandle, entry: LogEntry) -> Result<(), String> {
    let app_dir = get_log_dir(&app)?;
    let log_path = app_dir.join("desktop.log");

    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("无法创建日志目录: {e}"))?;
    }

    let message = if entry.message.len() > 8192 {
        format!("{}…(已截断)", &entry.message[..8192])
    } else {
        entry.message.clone()
    };

    let mut line = format!(
        "[{}] [{}] {}\n",
        entry.timestamp,
        entry.level.to_uppercase(),
        message
    );
    if let Some(ref stack) = entry.stack {
        line.push_str(&format!("  堆栈:\n{}\n", stack));
    }
    let bytes_to_write = line.len() as u64;

    if should_rotate(&log_path, bytes_to_write) {
        rotate(&log_path)?;
    }

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("无法打开日志文件 {:?}: {e}", log_path))?;

    file.write_all(line.as_bytes())
        .map_err(|e| format!("写入日志失败: {e}"))?;

    Ok(())
}

/// 判断当前日志文件在追加 `bytes_to_write` 后是否超过单文件上限。
///
/// 参数:
///     log_path: 当前日志文件路径。
///     bytes_to_write: 本次将要写入的字节数。
///
/// 返回:
///     当追加后文件大小将超过 `MAX_LOG_SIZE` 时返回 `true`；
///     文件不存在或元数据读取失败时返回 `false`（按不轮转处理）。
///
/// 异常:
///     无；元数据读取失败时按「不轮转」处理。
///
/// 副作用:
///     无。
fn should_rotate(log_path: &PathBuf, bytes_to_write: u64) -> bool {
    match fs::metadata(log_path) {
        Ok(meta) => meta.len() + bytes_to_write > MAX_LOG_SIZE,
        Err(_) => false,
    }
}

/// 将当前日志文件轮转为带编号的历史分片。
///
/// 先删除超出保留上限的最旧分片 `desktop.log.{MAX_BACKUPS+1}`（若存在），
/// 再从最大编号 `MAX_BACKUPS` 向前逐级位移（`desktop.log.{n}` → `desktop.log.{n+1}`），
/// 最后当前 `desktop.log` 变为 `desktop.log.1`。保留份数恒为 `MAX_BACKUPS`。
///
/// 参数:
///     log_path: 当前日志文件路径（即 `desktop.log`）。
///
/// 返回:
///     成功时返回 `Ok(())`。
///
/// 异常:
///     当任一删除、重命名操作失败时返回错误字符串。
///
/// 副作用:
///     删除/重命名磁盘上的日志分片文件。
fn rotate(log_path: &PathBuf) -> Result<(), String> {
    // 先删除超出保留上限的最旧分片，避免 Windows 下 rename 目标已存在而失败，
    // 也防止备份份数无限增长。
    let oldest = backup_path(log_path, MAX_BACKUPS + 1);
    if oldest.exists() {
        fs::remove_file(&oldest)
            .map_err(|e| format!("日志分片删除最旧分片失败 {:?}: {e}", oldest))?;
    }

    for n in (1..=MAX_BACKUPS).rev() {
        let src = backup_path(log_path, n);
        let dst = backup_path(log_path, n + 1);
        if src.exists() {
            fs::rename(&src, &dst)
                .map_err(|e| format!("日志分片轮转失败 {:?} → {:?}: {e}", src, dst))?;
        }
    }
    let first = backup_path(log_path, 1);
    fs::rename(log_path, &first)
        .map_err(|e| format!("日志分片轮转失败 {:?} → {:?}: {e}", log_path, first))?;
    Ok(())
}

/// 构造分片历史文件的完整路径。
///
/// 基于当前日志文件路径的父目录，在完整文件名（含 `.log` 段，如 `desktop.log`）
/// 后追加 `.{n}`，拼接出形如 `desktop.log.{n}` 的分片名。
/// 不使用 `file_stem`（会把 `desktop.log` 的 stem 误判为 `desktop`），
/// 也不使用 `with_extension`（在已含多段扩展名上会产生 `desktop.log.log.2` 之类错误名）。
///
/// 参数:
///     log_path: 当前日志文件路径（即 `desktop.log`）。
///     n: 分片编号（从 1 开始）。
///
/// 返回:
///     `desktop.log.{n}` 的完整路径。
///
/// 异常:
///     无。
///
/// 副作用:
///     无。
fn backup_path(log_path: &PathBuf, n: u32) -> PathBuf {
    let base = log_path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "desktop.log".to_string());
    let file_name = format!("{}.{}", base, n);
    log_path.with_file_name(file_name)
}

/// 获取前端日志目录路径。
///
/// 统一使用仓库根 `logs/` 目录（与后端日志目录约定一致），
/// 保证开发期与生产期日志路径均可预测、便于排查。
///
/// 参数:
///     _app: Tauri 应用句柄（保留以兼容命令调用签名）。
///
/// 返回:
///     仓库根 `logs/` 目录路径。
///
/// 异常:
///     当仓库根目录解析失败时，返回错误字符串。
///
/// 副作用:
///     无。
pub(crate) fn get_log_dir(_app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let repo_root = crate::backend::runtime_locator::resolve_repo_root()?;
    Ok(repo_root.join("logs"))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 为每个测试用例生成独立的临时目录，避免并行测试共享目录造成状态污染。
    ///
    /// 参数:
    ///     name: 测试用例标识，用于拼接待唯一化的目录名。
    ///
    /// 返回:
    ///     基于系统临时目录、带用例名的独立目录路径。
    fn test_dir(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("coding_agent_logtest_{}", name))
    }

    /// 测试目的：验证 backup_path 文件名构造正确，确认使用 with_file_name 而非
    /// with_extension，避免产生 `desktop.log.log.2` 这类错误文件名。
    /// 可能发现的缺陷类型：文件名构造错误（多段扩展名被错误处理）。
    #[test]
    fn test_backup_path_constructs_correct_names() {
        let log_path = PathBuf::from("/tmp/logs/desktop.log");
        for n in 1..=6u32 {
            let p = backup_path(&log_path, n);
            assert_eq!(
                p.file_name().map(|s| s.to_string_lossy().into_owned()),
                Some(format!("desktop.log.{n}")),
                "backup_path 应生成 desktop.log.{n} 文件名"
            );
            // 关键陷阱：绝不能出现重复扩展名
            assert!(
                !p.to_string_lossy().contains("desktop.log.log"),
                "backup_path 不应产生 desktop.log.log.* 这类错误文件名"
            );
        }
    }

    /// 测试目的：验证 backup_path 保留原始父目录，不丢失路径前缀。
    /// 可能发现的缺陷类型：父目录丢失或路径拼接错误。
    #[test]
    fn test_backup_path_preserves_parent_dir() {
        let log_path = PathBuf::from("C:\\Users\\me\\logs\\desktop.log");
        let p = backup_path(&log_path, 3);
        assert_eq!(p.parent().map(|p| p.to_string_lossy()), Some(std::borrow::Cow::Borrowed("C:\\Users\\me\\logs")));
        assert_eq!(p.file_name().map(|s| s.to_string_lossy()), Some(std::borrow::Cow::Borrowed("desktop.log.3")));
    }

    /// 测试目的：验证 rotate 在预置 desktop.log + desktop.log.1..5 后，
    /// 所有分片正确逐级 +1，且最旧的 desktop.log.5 被轮转为 desktop.log.6，
    /// 原 desktop.log 变为 desktop.log.1。
    /// 可能发现的缺陷类型：轮转顺序错误、编号偏移错误、文件覆盖。
    #[test]
    fn test_rotate_shifts_all_backups_up() {
        let dir = test_dir("shifts");
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::create_dir_all(&dir);
        let log_path = dir.join("desktop.log");

        // 预置文件，标记每个文件内容以示区分
        for n in 1..=5u32 {
            std::fs::write(backup_path(&log_path, n), format!("backup-{n}")).unwrap();
        }
        std::fs::write(&log_path, "current").unwrap();

        rotate(&log_path).unwrap();

        // 原 desktop.log 已被重命名为 desktop.log.1，原文件不应残留
        assert!(!log_path.exists(), "原 desktop.log 应已被重命名为 desktop.log.1，不应残留原文件");
        assert_eq!(
            std::fs::read_to_string(backup_path(&log_path, 1)).unwrap(),
            "current",
            "desktop.log.1 应包含原 desktop.log 的内容"
        );
        // 原 desktop.log.5 变成 desktop.log.6
        assert_eq!(
            std::fs::read_to_string(backup_path(&log_path, 6)).unwrap(),
            "backup-5",
            "desktop.log.6 应包含原 desktop.log.5 的内容"
        );
        // 中间逐级 +1
        for n in 2..=5u32 {
            assert_eq!(
                std::fs::read_to_string(backup_path(&log_path, n)).unwrap(),
                format!("backup-{}", n - 1),
                "desktop.log.{n} 应包含原 desktop.log.{} 的内容", n - 1
            );
        }
    }

    /// 测试目的：验证 rotate 当已存在 desktop.log.6（超过 MAX_BACKUPS）时，
    /// 最旧的 desktop.log.6 应被删除（而非无限累积）。
    /// 可能发现的缺陷类型：最旧分片未被删除，导致历史文件无限增长。
    #[test]
    fn test_rotate_deletes_oldest_when_exceeding_max() {
        let dir = test_dir("oldest");
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::create_dir_all(&dir);
        let log_path = dir.join("desktop.log");

        // 预置 desktop.log.1..6（模拟此前已轮转到上限）
        std::fs::write(backup_path(&log_path, 6), "oldest-6").unwrap();
        for n in 1..=5u32 {
            std::fs::write(backup_path(&log_path, n), format!("backup-{n}")).unwrap();
        }
        std::fs::write(&log_path, "current").unwrap();

        rotate(&log_path).unwrap();

        // desktop.log.6 被新内容覆盖（原 oldest-6 应被删除/替换），不应残留 "oldest-6"
        assert_eq!(
            std::fs::read_to_string(backup_path(&log_path, 6)).unwrap(),
            "backup-5",
            "超过上限后最旧的 desktop.log.6 应被删除并被新轮转内容替代"
        );
        // 确认未产生 desktop.log.7（轮转上限约束）
        assert!(!backup_path(&log_path, 7).exists(), "不应产生 desktop.log.7，轮转上限为 6");
    }

    /// 测试目的：验证 rotate 在仅存在 desktop.log（无历史分片）时也能正常工作。
    /// 可能发现的缺陷类型：空历史分片场景下 panic 或错误重命名。
    #[test]
    fn test_rotate_with_no_existing_backups() {
        let dir = test_dir("none");
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::create_dir_all(&dir);
        let log_path = dir.join("desktop.log");
        std::fs::write(&log_path, "only-current").unwrap();

        rotate(&log_path).unwrap();

        assert_eq!(
            std::fs::read_to_string(backup_path(&log_path, 1)).unwrap(),
            "only-current",
            "无历史分片时应直接生成 desktop.log.1"
        );
        assert!(!log_path.exists(), "原 desktop.log 不应残留");
    }

    /// 测试目的：验证 should_rotate 在现有文件大小 + 本次写入超过 MAX_LOG_SIZE 时返回 true。
    /// 可能发现的缺陷类型：阈值判断符号错误（误用 < 或 >=）、溢出。
    #[test]
    fn test_should_rotate_true_when_exceeds_max() {
        let dir = test_dir("exceeds");
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::create_dir_all(&dir);
        let log_path = dir.join("desktop.log");
        // 写 5MB - 100 字节，再追加 200 字节 => 超过上限
        let existing = vec![b'a'; (MAX_LOG_SIZE - 100) as usize];
        std::fs::write(&log_path, &existing).unwrap();

        assert!(
            should_rotate(&log_path, 200),
            "现有大小 + 写入字节 超过 MAX_LOG_SIZE 时应返回 true"
        );
    }

    /// 测试目的：验证 should_rotate 在未超过 MAX_LOG_SIZE 时返回 false（含恰好等于边界）。
    /// 可能发现的缺陷类型：边界条件错误（= 上限时误判轮转）。
    #[test]
    fn test_should_rotate_false_when_within_max() {
        let dir = test_dir("within");
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::create_dir_all(&dir);
        let log_path = dir.join("desktop.log");
        let existing = vec![b'b'; (MAX_LOG_SIZE - 100) as usize];
        std::fs::write(&log_path, &existing).unwrap();

        assert!(
            !should_rotate(&log_path, 50),
            "现有大小 + 写入字节 未超过 MAX_LOG_SIZE 时应返回 false"
        );
        // 恰好等于上限（差 100 + 写入 100 = 上限）边界不触发
        assert!(
            !should_rotate(&log_path, 100),
            "恰好等于 MAX_LOG_SIZE（未超过）时应返回 false"
        );
    }

    /// 测试目的：验证 should_rotate 在文件不存在时返回 false（满足 doc 契约）。
    /// 可能发现的缺陷类型：文件不存在时错误处理分支错误（误返回 true 或 panic）。
    #[test]
    fn test_should_rotate_false_when_file_missing() {
        let dir = test_dir("missing");
        let log_path = dir.join("nonexistent_desktop.log");
        let _ = std::fs::remove_file(&log_path);

        assert!(
            !should_rotate(&log_path, 100),
            "文件不存在时应返回 false"
        );
    }
}
