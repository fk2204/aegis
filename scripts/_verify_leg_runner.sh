#!/usr/bin/env bash
# AEGIS verify-bedrock leg runner — runs on the Hetzner box.
#
# Wraps run_corpus_bedrock.py so the long-running Bedrock call is
# decoupled from the SSH transport. The harness on the operator's
# laptop launches this via `nohup ... &` (the launching SSH exits in
# seconds) and then polls for the .done marker — the corpus run
# itself survives any number of transport drops.
#
# Drops these files in $REMOTE_DIR:
#   ${LEG}.stdout.log   — python stdout
#   ${LEG}.stderr.log   — python stderr (Bedrock httpx logs etc)
#   ${LEG}.exitcode     — captured exit code of the python invocation
#   ${LEG}.done         — touched AFTER exitcode is written (poll trigger)
#
# Args (positional):
#   $1 = leg name (baseline | pageroute)
#   $2 = AEGIS_PARSER_PAGE_ROUTING value (0 | 1)
#   $3 = output JSON path (absolute)
#   $4 = corpus root path (absolute)
#   $5 = remote_dir (where status files live)
# Optional:
#   $6 = --limit value (if set, passed through to run_corpus_bedrock.py)

set -u

LEG="$1"
PAGE_ROUTING="$2"
OUT_JSON="$3"
CORPUS_ROOT="$4"
REMOTE_DIR="$5"
LIMIT="${6:-}"

DONE_FILE="${REMOTE_DIR}/${LEG}.done"
EXITCODE_FILE="${REMOTE_DIR}/${LEG}.exitcode"
STDOUT_LOG="${REMOTE_DIR}/${LEG}.stdout.log"
STDERR_LOG="${REMOTE_DIR}/${LEG}.stderr.log"

# Pre-seed exitcode with a sentinel so a poll that races ahead of us
# never sees an empty file. Overwritten with the real exit code
# before the .done marker lands.
echo 255 > "$EXITCODE_FILE"

# Load the box's env (BEDROCK creds, model id, AEGIS_* config).
set -a
# shellcheck disable=SC1091
source /etc/aegis/aegis.env
set +a

# uv-resolved venv anchor.
cd /opt/aegis

LIMIT_ARGS=()
if [ -n "$LIMIT" ]; then
    LIMIT_ARGS=(--limit "$LIMIT")
fi

AEGIS_PARSER_PAGE_ROUTING="$PAGE_ROUTING" \
    uv run python "${REMOTE_DIR}/run_corpus_bedrock.py" \
    --out "$OUT_JSON" \
    --corpus-root "$CORPUS_ROOT" \
    "${LIMIT_ARGS[@]}" \
    > "$STDOUT_LOG" 2> "$STDERR_LOG"
RC=$?

# Write exitcode FIRST, then the done marker. Poll trusts: if .done
# exists then .exitcode is fully written.
echo "$RC" > "$EXITCODE_FILE"
touch "$DONE_FILE"
exit "$RC"
