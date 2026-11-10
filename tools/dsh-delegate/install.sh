#!/usr/bin/env bash
# Copy the plugin to a stable path and link the SKILL into both agent homes.
# Run from anywhere; the source stays this checkout.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One directory per version: a running host caches the JS module by file URL,
# so a live upgrade (remove_bundle + install_bundle, no app restart) needs a
# fresh path. A normal upgrade can still keep the profile on one path and
# restart the app instead.
plugin_version="$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$here/plugin/package.json" | head -n 1)"
plugin_dir="${DSH_DELEGATE_PLUGIN_DIR:-$HOME/dsh-plugins/dsh-delegate-${plugin_version:-dev}}"
agents_home="${DSH_AGENTS_HOME:-$HOME/.agents}"
skill_dir="$agents_home/skills/dsh-delegate"

echo "==> copying plugin to $plugin_dir"
mkdir -p "$(dirname "$plugin_dir")"
rm -rf "$plugin_dir"
cp -R "$here/plugin" "$plugin_dir"

echo "==> linking skill into $skill_dir"
mkdir -p "$(dirname "$skill_dir")"
rm -rf "$skill_dir"
cp -R "$here/skill" "$skill_dir"

link_skill() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  ln -sfn "$skill_dir" "$target"
  echo "    $target -> $skill_dir"
}

link_skill "$HOME/.claude/skills/dsh-delegate"
link_skill "$HOME/.codex/skills/dsh-delegate"

cat <<EOF

==> next
  Live upgrade without restarting the app (new path = new module generation):
    plugin_manager remove_bundle  target="dsh-delegate"
    plugin_manager install_bundle target="$plugin_dir"

  Or install once and restart the app afterwards:
    dsh plugin --profile desktop add "$plugin_dir"

  Verify (after the plugin is live):
       python3 "$skill_dir/scripts/dsh_delegate.py" health
  3. try it:
       python3 "$skill_dir/scripts/dsh_delegate.py" workspaces

Plugin directory (a link: dependency after install — keep it where it is):
  $plugin_dir
EOF
