#!/bin/sh
# Review a branch's diff against main using the Grok CLI.
#
# Usage:
#   scripts/grok-review.sh                  # current branch vs main
#   scripts/grok-review.sh some-branch       # some-branch vs main
#   scripts/grok-review.sh some-branch other  # some-branch vs other
#
# Runs interactively in your own terminal — you'll see Grok's reasoning
# live. Requires the grok CLI (https://x.ai) on PATH or at
# ~/.grok/bin/grok.exe.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

HEAD_REF="${1:-$(git rev-parse --abbrev-ref HEAD)}"
BASE_REF="${2:-main}"

GROK="$(command -v grok || true)"
if [ -z "$GROK" ]; then
  WIN_DEFAULT="$USERPROFILE/.grok/bin/grok.exe"
  if [ -f "$WIN_DEFAULT" ]; then
    GROK="$WIN_DEFAULT"
  else
    echo "grok CLI not found on PATH or at $WIN_DEFAULT" >&2
    exit 3
  fi
fi

git fetch origin "$BASE_REF" "$HEAD_REF" >/dev/null 2>&1 || true

PROMPT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE"' EXIT

cat > "$PROMPT_FILE" <<EOF
You are reviewing a git diff from the MeshChat-Windows project (a PySide6/Qt
desktop app for Meshtastic mesh radios). This is the diff of "$HEAD_REF"
against "$BASE_REF".

Review this diff for real, concrete bugs — not style nitpicks. Pay
particular attention to:
1. Qt signal/slot correctness — wrong signatures, double-connections,
   connecting to a destroyed object, wrong thread affinity.
2. The Leaflet/JS map code, if touched — marker/tooltip/cluster lifecycle,
   XSS via unescaped data reaching innerHTML/bindTooltip/bindPopup.
3. Any regression vs main, vs anything already broken before this diff (say
   which is which).
4. Anything else concretely wrong.

Output format: a short list of concrete findings, each with file:line,
severity (BLOCKER/MINOR/NIT), and a one-line fix suggestion. If genuinely
clean, say CLEAN. Do not restate the diff back. Do not suggest
style/formatting changes.

Here is the diff:

EOF

git diff "origin/$BASE_REF...origin/$HEAD_REF" >> "$PROMPT_FILE" 2>/dev/null \
  || git diff "$BASE_REF...$HEAD_REF" >> "$PROMPT_FILE"

"$GROK" --prompt-file "$PROMPT_FILE"
