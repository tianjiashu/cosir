use std::io::{self, Write};
use std::sync::{Arc, Mutex};

use crate::protocol::{encode_event, WorkerEvent};

pub type SharedFrameWriter = Arc<FrameWriter<io::Stdout>>;

pub struct FrameWriter<W> {
    writer: Mutex<W>,
}

impl<W: Write> FrameWriter<W> {
    pub fn new(writer: W) -> Self {
        Self {
            writer: Mutex::new(writer),
        }
    }

    pub fn send(&self, event: &WorkerEvent) -> anyhow::Result<()> {
        let frame = encode_event(event)?;
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| anyhow::anyhow!("frame writer lock poisoned"))?;
        writer.write_all(&frame)?;
        writer.flush()?;
        Ok(())
    }
}
