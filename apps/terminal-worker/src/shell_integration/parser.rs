//! 复用 vte 识别 OSC；按字节流位置发出事件，不从普通提示符文字猜测命令边界。
use serde_json::{json, Value};
use vte::{Parser, Perform};

pub struct ShellParser {
    parser: Parser,
    token: String,
    last_prompt_id: Option<Vec<u8>>,
}
impl ShellParser {
    pub fn new(token: String) -> Self {
        Self {
            parser: Parser::new(),
            token,
            last_prompt_id: None,
        }
    }
    pub fn advance(&mut self, byte: u8) -> Option<Value> {
        let mut performer = Marker {
            token: &self.token,
            last_prompt_id: &mut self.last_prompt_id,
            event: None,
        };
        self.parser.advance(&mut performer, &[byte]);
        performer.event
    }
}
struct Marker<'a> {
    token: &'a str,
    /// bash 把标记挂在 PS1 里，readline 重画提示符（补全列表、窗口变化、bash 3.2 的
    /// 不可见字符重绘）会原样再输出一次；同一提示符编号只发一次 shell_prompt。
    /// zsh / PowerShell 不带编号，不受影响。
    last_prompt_id: &'a mut Option<Vec<u8>>,
    event: Option<Value>,
}
impl Perform for Marker<'_> {
    fn osc_dispatch(&mut self, params: &[&[u8]], _bell: bool) {
        if self.token.is_empty()
            || params.len() < 5
            || params[0] != b"633"
            || params[1] != b"Cosir"
            || params[2] != self.token.as_bytes()
        {
            return;
        }
        let kind = String::from_utf8_lossy(params[3]);
        let value = decode(params[4]);
        self.event = match kind.as_ref() {
            "ready" if params.len() == 6 => {
                Some(json!({"kind":"ready", "shell": value, "version":decode(params[5])}))
            }
            "unavailable" => Some(json!({"kind":"unavailable", "reason":value})),
            "command_started" => Some(json!({"kind":"command_started", "command":value})),
            "shell_prompt" => {
                if let Some(id) = params.get(5) {
                    if self.last_prompt_id.as_deref() == Some(*id) {
                        return;
                    }
                    *self.last_prompt_id = Some(id.to_vec());
                }
                Some(
                    json!({"kind":"shell_prompt", "cwd":if value.is_empty() { None } else { Some(value) }}),
                )
            }
            "command_returned" => {
                let code = value.parse::<i32>().ok();
                let success = match value.as_str() {
                    "success" => Some(true),
                    "failure" => Some(false),
                    _ => code.map(|v| v == 0),
                };
                Some(
                    json!({"kind":"command_returned", "result":{"success":success, "code":code, "code_kind":code.map(|_| "shell_status")}}),
                )
            }
            _ => None,
        };
    }
}
fn decode(value: &[u8]) -> String {
    let mut output = Vec::new();
    let mut i = 0;
    while i < value.len() {
        if value[i] == b'\\' && i + 1 < value.len() {
            if value[i + 1] == b'\\' {
                output.push(b'\\');
                i += 2;
                continue;
            }
            if value[i + 1] == b'x' && i + 3 < value.len() {
                if let Ok(text) = std::str::from_utf8(&value[i + 2..i + 4]) {
                    if let Ok(byte) = u8::from_str_radix(text, 16) {
                        output.push(byte);
                        i += 4;
                        continue;
                    }
                }
            }
        }
        output.push(value[i]);
        i += 1;
    }
    String::from_utf8_lossy(&output).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn markers_survive_every_byte_boundary_and_ignore_wrong_identity() {
        let mut parser = ShellParser::new("token".into());
        let bytes = b"before\x1b]633;Cosir;wrong;command_returned;0\x07\x1b]633;Cosir;token;command_started;echo a\\x3bb\x1b\\after\x1b]633;Cosir;token;command_returned;7\x07";
        let events: Vec<_> = bytes.iter().filter_map(|b| parser.advance(*b)).collect();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["command"], "echo a;b");
        assert_eq!(events[1]["result"]["code"], 7);
    }
    #[test]
    fn redrawn_prompt_with_same_id_is_reported_once() {
        let mut parser = ShellParser::new("t".into());
        let bytes = b"\x1b]633;Cosir;t;shell_prompt;/a;1\x07$ \r\r\x1b]633;Cosir;t;shell_prompt;/a;1\x07$ \x1b]633;Cosir;t;shell_prompt;/a;2\x07\x1b]633;Cosir;t;shell_prompt;/b\x07\x1b]633;Cosir;t;shell_prompt;/b\x07";
        let events: Vec<_> = bytes.iter().filter_map(|b| parser.advance(*b)).collect();
        let cwds: Vec<_> = events.iter().map(|e| e["cwd"].as_str().unwrap()).collect();
        // 编号相同视为重画；无编号（zsh / PowerShell）每次都发。
        assert_eq!(cwds, ["/a", "/a", "/b", "/b"]);
    }

    #[test]
    fn powershell_result_does_not_invent_native_exit_code() {
        let mut parser = ShellParser::new("t".into());
        let event = b"\x1b]633;Cosir;t;command_returned;failure\x07"
            .iter()
            .find_map(|b| parser.advance(*b))
            .unwrap();
        assert_eq!(event["result"]["success"], false);
        assert!(event["result"]["code"].is_null());
    }
}
