use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};

/// 向本机后端发一次极简 HTTP GET，状态 200 时返回响应体。
///
/// 宿主只需要探活与少量只读查询，刻意不引入 HTTP 客户端依赖：请求固定为
/// `HTTP/1.0` + `Connection: close`，使后端不会用 chunked 编码，读到 EOF 即得完整响应。
/// `timeout` 是整次请求（连接 + 读取）的总时限，调用方据此保证不会长时间阻塞。
///
/// 返回:
///     状态 200 时返回原样响应体；连接、超时、非 200 或响应格式异常时返回错误描述。
pub fn get(address: SocketAddr, path: &str, timeout: Duration) -> Result<String, String> {
    let deadline = Instant::now() + timeout;
    let mut stream =
        TcpStream::connect_timeout(&address, timeout).map_err(|error| error.to_string())?;
    let remaining = deadline
        .checked_duration_since(Instant::now())
        .filter(|value| !value.is_zero())
        .ok_or_else(|| "请求超时".to_string())?;
    stream
        .set_read_timeout(Some(remaining))
        .map_err(|error| error.to_string())?;
    stream
        .write_all(
            format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )
        .map_err(|error| error.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| error.to_string())?;
    parse_ok_body(&response)
}

fn parse_ok_body(response: &str) -> Result<String, String> {
    let (head, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "后端响应缺少头部分隔".to_string())?;
    let status_line = head.lines().next().unwrap_or_default();
    if status_line.split_whitespace().nth(1) != Some("200") {
        return Err(format!("后端返回非 200 状态：{status_line}"));
    }
    Ok(body.to_string())
}

#[cfg(test)]
mod tests {
    use super::parse_ok_body;

    #[test]
    fn ok_response_returns_body() {
        let body =
            parse_ok_body("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"count\":2}")
                .unwrap();
        assert_eq!(body, "{\"count\":2}");
    }

    #[test]
    fn non_ok_response_is_error() {
        assert!(parse_ok_body("HTTP/1.1 404 Not Found\r\n\r\n{}").is_err());
        assert!(parse_ok_body("garbage").is_err());
    }
}
