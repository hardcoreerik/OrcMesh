#!/bin/sh
# Review a branch's diff against main using the Grok CLI, reusing a persistent
# Grok session per branch instead of starting cold every time.
#
# Usage:
#   scripts/grok-review.sh                    # current branch vs main
#   scripts/grok-review.sh some-branch         # some-branch vs main
#   scripts/grok-review.sh some-branch main PR6-final-pass   # + a label
#
# Session/state files live OUTSIDE the repo (never committed) at
# $GROK_REVIEWS_DIR (default F:\AI\Temp\Grok-Reviews), keyed on branch+base
# together (reviewing the same branch against a different base is a
# different comparison and gets its own state):
#   <Project>_BASE.session-id                    shared repo-orientation session
#   <Project>_<branch>_vs_<base>.session-id       this comparison's forked review session
#   <Project>_<branch>_vs_<base>.last-commit      commit last reviewed for this comparison
#   <Project>_<branch>_vs_<base>_<label>_<ts>.prompt.txt   the prompt sent each run (for reference)
#
# First run on a branch: forks a fresh session off the shared base (creating
# the base session once, the first time this ever runs, by having Grok skim
# the repo for orientation) and reviews the full diff vs main.
# Later runs on the same branch: resume that session and send only the
# incremental diff since the last reviewed commit — Grok already has the
# earlier diff and findings in context, so there's no need to re-paste
# everything or have it re-derive context on every pass. If nothing changed
# since the last review, this exits immediately without calling Grok at all.
#
# Runs interactively in your own terminal by default. Set GROK_HEADLESS=1 to
# run non-interactively (used by the autonomous improvement loop).
#
# Requires the grok CLI (https://x.ai) on PATH or at ~/.grok/bin/grok.exe.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

HEAD_REF="${1:-$(git rev-parse --abbrev-ref HEAD)}"
BASE_REF="${2:-main}"
LABEL="${3:-review}"

GROK="$(command -v grok || true)"
if [ -z "$GROK" ]; then
  # ${VAR:-} guards against "unbound variable" under set -u when
  # USERPROFILE/HOME aren't set (e.g. non-Windows, or a stripped-down
  # shell) — an empty candidate just fails the -f check harmlessly instead
  # of crashing before we can print the real error.
  for candidate in "${USERPROFILE:-}/.grok/bin/grok.exe" "${HOME:-}/.grok/bin/grok.exe" "${HOME:-}/.grok/bin/grok"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
      GROK="$candidate"
      break
    fi
  done
  if [ -z "$GROK" ]; then
    echo "grok CLI not found on PATH, \$USERPROFILE/.grok/bin/grok.exe, or \$HOME/.grok/bin/grok(.exe)" >&2
    exit 3
  fi
fi

# ── Where session/state files live (outside the repo, never committed) ─────
GROK_REVIEWS_DIR="${GROK_REVIEWS_DIR:-/f/AI/Temp/Grok-Reviews}"
mkdir -p "$GROK_REVIEWS_DIR"

REMOTE_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
if [ -n "$REMOTE_URL" ]; then
  PROJECT_NAME="$(basename "$REMOTE_URL" .git)"
else
  PROJECT_NAME="$(basename "$REPO_ROOT")"
fi
BRANCH_SLUG="$(printf '%s' "$HEAD_REF" | tr '/' '-')"
BASE_REF_SLUG="$(printf '%s' "$BASE_REF" | tr '/' '-')"
# Keyed on branch+base together: reviewing the same branch against a
# different base ref is a different comparison and must not reuse a
# last-reviewed-commit checkpoint (or skip entirely) from a prior run
# against a different base.
COMPARISON_SLUG="${BRANCH_SLUG}_vs_${BASE_REF_SLUG}"

BASE_SESSION_FILE="$GROK_REVIEWS_DIR/${PROJECT_NAME}_BASE.session-id"
BRANCH_SESSION_FILE="$GROK_REVIEWS_DIR/${PROJECT_NAME}_${COMPARISON_SLUG}.session-id"
LAST_COMMIT_FILE="$GROK_REVIEWS_DIR/${PROJECT_NAME}_${COMPARISON_SLUG}.last-commit"
TS="$(date +%Y%m%d-%H%M%S)"
PROMPT_FILE="$GROK_REVIEWS_DIR/${PROJECT_NAME}_${COMPARISON_SLUG}_${LABEL}_${TS}.prompt.txt"

new_uuid() {
  if command -v python >/dev/null 2>&1; then
    python -c "import uuid; print(uuid.uuid4())"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import uuid; print(uuid.uuid4())"
  else
    powershell -NoProfile -Command "[guid]::NewGuid().ToString()"
  fi
}

if [ "${GROK_HEADLESS:-0}" = "1" ]; then
  MODE_FLAGS="--always-approve --no-alt-screen --output-format plain"
else
  MODE_FLAGS=""
fi

git fetch origin "$BASE_REF" "$HEAD_REF" >/dev/null 2>&1 || true

# Prefer the local branch tip when it exists — origin/$HEAD_REF can lag
# behind local commits that haven't been pushed yet, which would otherwise
# silently exclude them from both the full diff and the incremental-since-
# last-review range. Fall back to the remote-tracking ref only when there's
# no local branch (e.g. reviewing someone else's branch by name).
if git show-ref --verify --quiet "refs/heads/$HEAD_REF"; then
  CURRENT_REF="$HEAD_REF"
else
  CURRENT_REF="origin/$HEAD_REF"
fi

# ── One-time shared base session: orient Grok on the codebase once, so every
#    branch's forked session starts warm instead of cold. ─────────────────
# Core project docs to ground every review against — in-repo docs plus the
# original spec/design documents that live one level up, outside git (they
# predate/motivated this repo and aren't meant to be committed into it).
DOC_PATHS="$REPO_ROOT/README.md $REPO_ROOT/ARCHITECTURE.md $REPO_ROOT/ROADMAP.md"
for f in "$REPO_ROOT"/docs/*.md; do
  [ -f "$f" ] && DOC_PATHS="$DOC_PATHS $f"
done
for f in "$REPO_ROOT"/../*.md; do
  [ -f "$f" ] && DOC_PATHS="$DOC_PATHS $f"
done

if [ ! -f "$BASE_SESSION_FILE" ]; then
  BASE_ID="$(new_uuid)"
  echo "First-ever run for $PROJECT_NAME — setting up a shared base review session..." >&2
  if "$GROK" --session-id "$BASE_ID" --always-approve --no-alt-screen --output-format plain \
      -p "Orientation only, no review needed yet. Read these core project documents to understand the intended architecture, roadmap, and phasing before you look at any code: $DOC_PATHS — then skim this repo's structure and its most-changed/most-central source files to build general context you'll reuse across future PR reviews in this session tree. Summarize the architecture and current roadmap status in a few bullet points." \
      >/dev/null 2>&1
  then
    echo "$BASE_ID" > "$BASE_SESSION_FILE"
  else
    echo "Warning: base session setup failed — branch sessions will start standalone instead of forking from a warm base." >&2
  fi
fi
BASE_ID="$(cat "$BASE_SESSION_FILE" 2>/dev/null || true)"

# ── This branch's session: fork from base on first use, else resume as-is ──
if [ -f "$BRANCH_SESSION_FILE" ]; then
  BRANCH_ID="$(cat "$BRANCH_SESSION_FILE")"
  RESUME_ARGS="--resume $BRANCH_ID"
  FIRST_RUN=0
else
  BRANCH_ID="$(new_uuid)"
  echo "$BRANCH_ID" > "$BRANCH_SESSION_FILE"
  if [ -n "$BASE_ID" ]; then
    RESUME_ARGS="--resume $BASE_ID --fork-session --session-id $BRANCH_ID"
  else
    RESUME_ARGS="--session-id $BRANCH_ID"
  fi
  FIRST_RUN=1
fi

# ── Build the diff: full diff on first run, incremental since last reviewed
#    commit on later runs. If nothing changed, skip calling Grok entirely. ─
if [ "$FIRST_RUN" = 1 ] || [ ! -f "$LAST_COMMIT_FILE" ]; then
  DIFF_RANGE="origin/$BASE_REF...$CURRENT_REF"
  DIFF_CONTENT="$(git diff "$DIFF_RANGE" 2>/dev/null || git diff "$BASE_REF...$CURRENT_REF")"
  DIFF_DESC="the full diff of \"$HEAD_REF\" against \"$BASE_REF\""
else
  LAST_COMMIT="$(cat "$LAST_COMMIT_FILE")"
  CURRENT_COMMIT="$(git rev-parse "$CURRENT_REF")"
  if [ "$LAST_COMMIT" = "$CURRENT_COMMIT" ]; then
    echo "No new commits on \"$HEAD_REF\" since the last review ($LAST_COMMIT). Nothing to review."
    exit 0
  fi
  DIFF_CONTENT="$(git diff "$LAST_COMMIT..$CURRENT_COMMIT" 2>/dev/null || true)"
  DIFF_DESC="only what changed since your last review of this branch (commit $LAST_COMMIT..$CURRENT_COMMIT) — you already have the earlier diff and your prior findings in context, so focus on: did the earlier findings actually get fixed, and is there anything new in this incremental change"
fi

cat > "$PROMPT_FILE" <<EOF
You are reviewing a git diff from the "$PROJECT_NAME" project. This is $DIFF_DESC.

Review for real, concrete bugs — not style nitpicks. Pay particular attention
to: signal/slot or event-wiring correctness, thread-safety/lifecycle issues,
XSS/escaping on any string reaching HTML/innerHTML, and regressions vs the
base branch (say explicitly whether each finding is a regression from this
diff or pre-existing).

Also check this diff against the project's core documents you read during
orientation (ARCHITECTURE.md, ROADMAP.md, README.md, and the original spec
docs) — flag anything that contradicts documented architecture/conventions,
duplicates an already-deferred/out-of-scope item, or completes/changes
something ARCHITECTURE.md or ROADMAP.md should be updated to reflect.

Output format: a short list of concrete findings, each with file:line,
severity (BLOCKER/MINOR/NIT), and a one-line fix suggestion. If genuinely
clean, say CLEAN. Do not restate the diff back. Do not suggest
style/formatting changes.

Here is the diff:

$DIFF_CONTENT
EOF

# shellcheck disable=SC2086
"$GROK" $RESUME_ARGS $MODE_FLAGS --prompt-file "$PROMPT_FILE"

git rev-parse "$CURRENT_REF" > "$LAST_COMMIT_FILE"
