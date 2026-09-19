mod io;
mod lifecycle;
mod process_tree;
mod protocol;
mod pty;
mod terminal_emulator;
mod worker;

fn main() {
    let result = run();
    if let Err(error) = result {
        eprintln!("terminal worker failed: {error:#}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    let mut args = std::env::args_os().skip(1);
    let mut stdio = false;
    let mut instance_id = None;
    while let Some(argument) = args.next() {
        match argument.to_string_lossy().as_ref() {
            "--stdio" => stdio = true,
            "--instance-id" => {
                instance_id = args
                    .next()
                    .map(|value| value.to_string_lossy().into_owned());
            }
            _ => anyhow::bail!("unknown terminal worker argument"),
        }
    }
    if !stdio {
        anyhow::bail!("terminal worker requires --stdio")
    }
    let instance_id = instance_id.ok_or_else(|| anyhow::anyhow!("missing --instance-id"))?;
    worker::run(instance_id)
}
