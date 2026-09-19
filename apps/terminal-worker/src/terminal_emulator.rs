use std::time::{Duration, Instant};
use vte::{Params, Parser, Perform};

use crate::pty::PtyRuntime;

const DEFAULT_CURSOR_ROW: u16 = 1;
const DEFAULT_CURSOR_COLUMN: u16 = 1;
const MAX_CURSOR_POSITION: u16 = 32_767;
const MAX_PENDING_SEQUENCE_AGE: Duration = Duration::from_secs(5);

#[derive(Clone, Copy)]
enum PendingSequence {
    Escape,
    Csi,
    Osc,
    Dcs,
    StringEscape(PendingString),
}

#[derive(Clone, Copy)]
enum PendingString {
    Osc,
    Dcs,
}

/// The small part of terminal-emulator state needed for safe DSR replies.
///
/// This is intentionally not a second full terminal emulator. The mature `vte`
/// parser owns escape-sequence framing and this type tracks only cursor
/// position and the DSR policy required by the worker. It is enabled only for
/// shells whose startup sequences are known to expect terminal semantics.
pub struct TerminalQueryResponder {
    parser: Parser,
    cursor_row: u16,
    cursor_column: u16,
    enabled: bool,
    response_disabled: bool,
    pending_sequence: Option<PendingSequence>,
    pending_since: Option<Instant>,
}

impl TerminalQueryResponder {
    pub fn new(enabled: bool) -> Self {
        Self {
            parser: Parser::new(),
            cursor_row: DEFAULT_CURSOR_ROW,
            cursor_column: DEFAULT_CURSOR_COLUMN,
            enabled,
            response_disabled: false,
            pending_sequence: None,
            pending_since: None,
        }
    }

    /// Parse one output chunk and, when enabled, answer complete DSR queries.
    ///
    /// A response write failure disables future automatic replies but does not
    /// fail the PTY output stream. The caller can report the degradation as a
    /// non-fatal worker event while preserving the shell session.
    pub fn process(&mut self, runtime: &PtyRuntime, data: &[u8]) -> bool {
        let mut write_response = |response: &[u8]| runtime.write(response).is_ok();
        self.process_with_writer(data, &mut write_response)
    }

    fn process_with_writer(
        &mut self,
        data: &[u8],
        write_response: &mut impl FnMut(&[u8]) -> bool,
    ) -> bool {
        if !self.enabled || self.response_disabled {
            return true;
        }

        let now = Instant::now();
        if self
            .pending_since
            .is_some_and(|started| now.duration_since(started) > MAX_PENDING_SEQUENCE_AGE)
        {
            self.parser = Parser::new();
            self.pending_sequence = None;
            self.pending_since = None;
        }
        self.update_pending_sequence(data, now);

        let mut parser = std::mem::take(&mut self.parser);
        let mut performer = QueryPerformer {
            cursor_row: &mut self.cursor_row,
            cursor_column: &mut self.cursor_column,
            write_response,
            response_failed: false,
        };
        parser.advance(&mut performer, data);
        self.parser = parser;

        if performer.response_failed {
            self.response_disabled = true;
            return false;
        }
        true
    }

    fn update_pending_sequence(&mut self, data: &[u8], now: Instant) {
        let was_pending = self.pending_sequence.is_some();
        let mut state = self.pending_sequence;
        for &byte in data {
            state = match state {
                None if byte == 0x1b => Some(PendingSequence::Escape),
                None => None,
                Some(PendingSequence::Escape) => match byte {
                    b'[' => Some(PendingSequence::Csi),
                    b']' => Some(PendingSequence::Osc),
                    b'P' => Some(PendingSequence::Dcs),
                    0x1b => Some(PendingSequence::Escape),
                    _ => None,
                },
                Some(PendingSequence::Csi) => {
                    if (0x40..=0x7e).contains(&byte) || matches!(byte, 0x18 | 0x1a) {
                        None
                    } else {
                        Some(PendingSequence::Csi)
                    }
                }
                Some(PendingSequence::Osc) => match byte {
                    0x07 => None,
                    0x1b => Some(PendingSequence::StringEscape(PendingString::Osc)),
                    0x18 | 0x1a => None,
                    _ => Some(PendingSequence::Osc),
                },
                Some(PendingSequence::Dcs) => match byte {
                    0x1b => Some(PendingSequence::StringEscape(PendingString::Dcs)),
                    0x18 | 0x1a => None,
                    _ => Some(PendingSequence::Dcs),
                },
                Some(PendingSequence::StringEscape(kind)) => {
                    if byte == b'\\' {
                        None
                    } else if byte == 0x1b {
                        Some(PendingSequence::StringEscape(kind))
                    } else {
                        Some(match kind {
                            PendingString::Osc => PendingSequence::Osc,
                            PendingString::Dcs => PendingSequence::Dcs,
                        })
                    }
                }
            };
        }
        self.pending_sequence = state;
        self.pending_since = match (was_pending, state) {
            (_, None) => None,
            (false, Some(_)) => Some(now),
            (true, Some(_)) => self.pending_since.or(Some(now)),
        };
    }
}

struct QueryPerformer<'a> {
    cursor_row: &'a mut u16,
    cursor_column: &'a mut u16,
    write_response: &'a mut dyn FnMut(&[u8]) -> bool,
    response_failed: bool,
}

impl QueryPerformer<'_> {
    fn set_cursor(&mut self, row: u16, column: u16) {
        *self.cursor_row = row.clamp(1, MAX_CURSOR_POSITION);
        *self.cursor_column = column.clamp(1, MAX_CURSOR_POSITION);
    }

    fn parameter(params: &Params, index: usize, default: u16) -> u16 {
        params
            .iter()
            .nth(index)
            .and_then(|group| group.first().copied())
            .filter(|value| *value != 0)
            .unwrap_or(default)
    }
}

impl Perform for QueryPerformer<'_> {
    fn print(&mut self, _character: char) {
        *self.cursor_column = self
            .cursor_column
            .saturating_add(1)
            .min(MAX_CURSOR_POSITION);
    }

    fn execute(&mut self, byte: u8) {
        match byte {
            b'\r' => *self.cursor_column = 1,
            b'\n' => *self.cursor_row = self.cursor_row.saturating_add(1).min(MAX_CURSOR_POSITION),
            0x08 => *self.cursor_column = self.cursor_column.saturating_sub(1).max(1),
            0x09 => {
                let next_tab = ((*self.cursor_column as u32).div_ceil(8)) * 8 + 1;
                *self.cursor_column = next_tab.min(MAX_CURSOR_POSITION as u32) as u16;
            }
            _ => {}
        }
    }

    fn csi_dispatch(&mut self, params: &Params, intermediates: &[u8], ignore: bool, action: char) {
        if ignore || !intermediates.is_empty() {
            return;
        }

        match action {
            'n' if params.iter().eq([[6_u16].as_slice()]) => {
                let response = format!("\x1b[{};{}R", self.cursor_row, self.cursor_column);
                if !(self.write_response)(response.as_bytes()) {
                    self.response_failed = true;
                }
            }
            'H' | 'f' => {
                self.set_cursor(Self::parameter(params, 0, 1), Self::parameter(params, 1, 1));
            }
            'A' => {
                let count = Self::parameter(params, 0, 1);
                *self.cursor_row = self.cursor_row.saturating_sub(count).max(1);
            }
            'B' => {
                let count = Self::parameter(params, 0, 1);
                *self.cursor_row = self
                    .cursor_row
                    .saturating_add(count)
                    .min(MAX_CURSOR_POSITION);
            }
            'C' => {
                let count = Self::parameter(params, 0, 1);
                *self.cursor_column = self
                    .cursor_column
                    .saturating_add(count)
                    .min(MAX_CURSOR_POSITION);
            }
            'D' => {
                let count = Self::parameter(params, 0, 1);
                *self.cursor_column = self.cursor_column.saturating_sub(count).max(1);
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::TerminalQueryResponder;

    #[test]
    fn query_split_across_output_chunks_is_reassembled() {
        let mut responder = TerminalQueryResponder::new(true);
        let mut responses = Vec::new();
        {
            let mut write_response = |response: &[u8]| {
                responses.push(response.to_vec());
                true
            };
            assert!(responder.process_with_writer(b"before\x1b[", &mut write_response));
        }
        assert!(responses.is_empty());
        let mut write_response = |response: &[u8]| {
            responses.push(response.to_vec());
            true
        };
        assert!(responder.process_with_writer(b"6nafter", &mut write_response));
        assert_eq!(responses, vec![b"\x1b[1;7R".to_vec()]);
    }

    #[test]
    fn strings_and_csi_sequences_do_not_break_a_later_dsr() {
        let mut responder = TerminalQueryResponder::new(true);
        let mut responses = Vec::new();
        let mut write_response = |response: &[u8]| {
            responses.push(response.to_vec());
            true
        };

        let data = b"\x1b]0;title\x07\x1bP1;2+qpayload\x1b\\\x1b[4;9H\x1b[6n";
        assert!(responder.process_with_writer(data, &mut write_response));
        assert_eq!(responses, vec![b"\x1b[4;9R".to_vec()]);
    }

    #[test]
    fn cursor_position_is_used_for_dsr_response() {
        let mut responder = TerminalQueryResponder::new(true);
        let mut responses = Vec::new();
        let mut write_response = |response: &[u8]| {
            responses.push(response.to_vec());
            true
        };

        assert!(responder.process_with_writer(b"\x1b[3;5H\x1b[6n", &mut write_response));
        assert_eq!(responses, vec![b"\x1b[3;5R".to_vec()]);
    }

    #[test]
    fn disabled_policy_does_not_inject_a_response() {
        let mut responder = TerminalQueryResponder::new(false);
        let mut responses = Vec::new();
        let mut write_response = |response: &[u8]| {
            responses.push(response.to_vec());
            true
        };

        assert!(responder.process_with_writer(b"\x1b[6n", &mut write_response));
        assert!(responses.is_empty());
    }

    #[test]
    fn response_failure_disables_future_responses_without_failing_parser() {
        let mut responder = TerminalQueryResponder::new(true);
        let mut write_response = |_response: &[u8]| false;
        assert!(!responder.process_with_writer(b"\x1b[6n", &mut write_response));

        let mut responses = Vec::new();
        let mut write_response = |response: &[u8]| {
            responses.push(response.to_vec());
            true
        };
        assert!(responder.process_with_writer(b"\x1b[6n", &mut write_response));
        assert!(responses.is_empty());
    }
}
