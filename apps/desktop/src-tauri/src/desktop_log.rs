use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const MAX_BYTES: u64 = 5 * 1024 * 1024;
const BACKUP_COUNT: u32 = 7;

pub fn append_json_line(path: &Path, mut entry: serde_json::Value) -> Result<(), String> {
    let lock = log_lock(path);
    let _guard = lock.lock().map_err(|_| "日志写入锁已损坏".to_string())?;
    append_json_line_locked(path, &mut entry)
}

pub fn append_console_line(path: &Path, stream: &str, line: &str) -> Result<(), String> {
    append_json_line(
        path,
        serde_json::json!({
            "level": "INFO",
            "logger": "coding_agent.backend_console",
            "trace_id": "",
            "caller": "backend_process",
            "event": "backend_console_output",
            "msg": line,
            "data": {"stream": stream},
            "error": null,
            "truncated": false
        }),
    )
}

fn append_json_line_locked(path: &Path, entry: &mut serde_json::Value) -> Result<(), String> {
    let object = entry
        .as_object_mut()
        .ok_or_else(|| "日志必须是 JSON 对象".to_string())?;
    object
        .entry("ts".to_string())
        .or_insert_with(|| serde_json::Value::String(timestamp()));
    let line = serde_json::to_string(&entry).map_err(|error| format!("日志序列化失败：{error}"))?;
    if line.len() as u64 > MAX_BYTES {
        return Err("日志单行超过大小上限".to_string());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("无法创建日志目录：{error}"))?;
    }
    let active_path = dated_log_path(path, &current_log_date())?;
    rotate_if_needed(&active_path, line.len() as u64 + 1)?;
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&active_path)
        .map_err(|error| format!("无法打开日志文件：{error}"))?;
    writeln!(file, "{line}").map_err(|error| format!("无法写入日志：{error}"))
}

fn dated_log_path(path: &Path, log_date: &str) -> Result<PathBuf, String> {
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "日志文件名不能为空".to_string())?;
    let extension = path.extension().and_then(|value| value.to_str());
    let file_name = match extension {
        Some(extension) => format!("{stem}-{log_date}.{extension}"),
        None => format!("{stem}-{log_date}"),
    };
    Ok(path.with_file_name(file_name))
}

fn shard_path(path: &Path, index: u32) -> PathBuf {
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("log");
    match path.extension().and_then(|value| value.to_str()) {
        Some(extension) => path.with_file_name(format!("{stem}.{index}.{extension}")),
        None => path.with_file_name(format!("{stem}.{index}")),
    }
}

fn rotate_if_needed(path: &Path, incoming_bytes: u64) -> Result<(), String> {
    let too_large = fs::metadata(path)
        .map(|metadata| metadata.len() > 0 && metadata.len() + incoming_bytes > MAX_BYTES)
        .unwrap_or(false);
    if !too_large {
        return Ok(());
    }
    let oldest = shard_path(path, BACKUP_COUNT);
    let _ = fs::remove_file(oldest);
    for index in (1..BACKUP_COUNT).rev() {
        let source = shard_path(path, index);
        let target = shard_path(path, index + 1);
        if source.exists() {
            let _ = fs::remove_file(&target);
            fs::rename(source, target).map_err(|error| format!("日志轮转失败：{error}"))?;
        }
    }
    let first = shard_path(path, 1);
    let _ = fs::remove_file(&first);
    fs::rename(path, first).map_err(|error| format!("日志轮转失败：{error}"))
}

fn current_log_date() -> String {
    time::OffsetDateTime::now_local()
        .unwrap_or_else(|_| time::OffsetDateTime::now_utc())
        .date()
        .to_string()
}

fn log_lock(path: &Path) -> &'static Mutex<()> {
    static DESKTOP_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    static FRONTEND_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    if path.file_name().and_then(|name| name.to_str()) == Some("frontend.log") {
        FRONTEND_LOCK.get_or_init(|| Mutex::new(()))
    } else {
        DESKTOP_LOCK.get_or_init(|| Mutex::new(()))
    }
}

fn timestamp() -> String {
    time::OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

#[cfg(test)]
mod tests {
    use super::{dated_log_path, rotate_if_needed, shard_path, MAX_BYTES};
    use std::fs;
    use std::path::PathBuf;

    fn test_directory() -> PathBuf {
        let directory = std::env::temp_dir().join(format!(
            "cosir-desktop-log-{}-{}",
            std::process::id(),
            time::OffsetDateTime::now_utc().unix_timestamp_nanos()
        ));
        fs::create_dir(&directory).expect("创建日志测试目录失败");
        directory
    }

    #[test]
    fn dated_log_path_keeps_name_and_extension() {
        let path = PathBuf::from("runtime/backend-console.log");
        assert_eq!(
            dated_log_path(&path, "2026-09-13").expect("日期路径生成失败"),
            PathBuf::from("runtime/backend-console-2026-09-13.log")
        );
    }

    #[test]
    fn rotates_before_incoming_line_exceeds_limit() {
        let directory = test_directory();
        let active = directory.join("frontend-2026-09-13.log");
        fs::write(&active, vec![b'x'; (MAX_BYTES - 1) as usize]).expect("写入测试日志失败");

        rotate_if_needed(&active, 2).expect("日志轮转失败");

        assert!(!active.exists());
        assert_eq!(
            fs::metadata(shard_path(&active, 1))
                .expect("分片不存在")
                .len(),
            MAX_BYTES - 1
        );
        fs::remove_dir_all(directory).expect("清理日志测试目录失败");
    }
}
