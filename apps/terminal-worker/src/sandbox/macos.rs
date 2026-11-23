use super::ExecutionPolicy;
use std::path::Path;
use std::process::Command;

fn quoted(path: &Path) -> anyhow::Result<String> {
    let value = path
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("sandbox path must be UTF-8"))?;
    Ok(serde_json::to_string(value)?)
}

fn subtree(path: &Path) -> anyhow::Result<String> {
    let path = quoted(path)?;
    Ok(format!("(require-any (literal {path}) (subpath {path}))"))
}

pub fn profile(policy: &ExecutionPolicy) -> anyhow::Result<String> {
    let mut text = include_str!("macos-base.sbpl").to_owned();
    // 每条 allow 都减去拒绝集合；不使用后加宽泛 allow 覆盖先前 deny 的策略。
    let read_exclusions = policy
        .denied_read
        .iter()
        .map(|path| {
            let exceptions = policy
                .readable_exceptions
                .iter()
                .filter(|p| p.starts_with(path))
                .map(|p| subtree(p))
                .collect::<anyhow::Result<Vec<_>>>()?;
            let region = subtree(path)?;
            Ok(if exceptions.is_empty() {
                format!("(require-not {region})")
            } else {
                format!(
                    "(require-not (require-all {region} (require-not (require-any {}))))",
                    exceptions.join(" ")
                )
            })
        })
        .collect::<anyhow::Result<Vec<_>>>()?;
    text.push_str(&format!(
        "\n(allow file-read* (require-all (subpath \"/\") {}))\n",
        read_exclusions.join(" ")
    ));
    let read_only = policy.read_only_paths()?;
    for root in &policy.writable_roots {
        let exclusions = read_only
            .iter()
            .map(|p| Ok(format!("(require-not {})", subtree(p)?)))
            .collect::<anyhow::Result<Vec<String>>>()?;
        text.push_str(&format!(
            "(allow file-write* (require-all {} {}))\n",
            subtree(root)?,
            exclusions.join(" ")
        ));
    }
    for root in &policy.writable_exceptions {
        text.push_str(&format!("(allow file-write* {})\n", subtree(root)?));
    }
    // 防止通过改名保护路径的祖先，或者只读 fd 的特殊 fcntl 写入绕过路径规则。
    for protected in &read_only {
        for path in protected.ancestors().filter(|p| p.parent().is_some()) {
            text.push_str(&format!(
                "(deny file-write-unlink (literal {}))\n",
                quoted(path)?
            ));
        }
    }
    text.push_str("(deny system-fcntl (fcntl-command 80) (fcntl-command 110))\n");
    if policy.network {
        text.push_str("(allow network-outbound (remote ip))\n(allow network-inbound network-bind (local ip))\n");
        text.push_str(include_str!("macos-network.sbpl"));
    }
    Ok(text)
}

pub fn command(
    program: &str,
    args: &[String],
    policy: &ExecutionPolicy,
) -> anyhow::Result<Command> {
    let mut command = Command::new("/usr/bin/sandbox-exec");
    command
        .args(["-p", &profile(policy)?, "--", program])
        .args(args);
    Ok(command)
}
