#!/usr/bin/env bash
# Safety Guard verdict. Runs from the BASE commit only (see safety-guard.yml).
# Usage: safety-guard.sh <changes.tsv> <patterns.txt>
#   changes.tsv: "<status>\t<filename>\t<previous_filename>" per line, from the PR files API.
# Env: REVIEWED=true|false (safety-reviewed label present)
set -uo pipefail

changes="$1"
pattern_file="$2"

patterns=()
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "${line// }" || "$line" == \#* ]] && continue
  patterns+=("$line")
done < "$pattern_file"
if [[ ${#patterns[@]} -eq 0 ]]; then
  echo "::error::No safety patterns loaded from $pattern_file. Failing closed."
  exit 1
fi
if [[ ! -s "$changes" ]]; then
  echo "::error::Could not read the PR file list. Failing closed."
  exit 1
fi

matches() {
  local path="$1" p
  [[ -z "$path" ]] && return 1
  for p in "${patterns[@]}"; do
    # shellcheck disable=SC2053
    [[ "$path" == $p ]] && return 0
  done
  return 1
}

hits=()
while IFS=$'\t' read -r status path previous; do
  if matches "$path" || matches "$previous"; then
    hits+=("$status $path${previous:+ (from $previous)}")
  fi
  if [[ "$status" == "removed" && "$path" == tests/* ]]; then
    hits+=("DELETED TEST $path")
  fi
  if [[ "$status" == "renamed" && "$previous" == tests/* && "$path" != tests/* ]]; then
    hits+=("TEST MOVED OUT $previous -> $path")
  fi
done < "$changes"

if [[ ${#hits[@]} -eq 0 ]]; then
  echo "No safety-critical paths touched."
  exit 0
fi

printf 'Safety-critical changes:\n'; printf '  %s\n' "${hits[@]}"
if [[ "${REVIEWED:-false}" == "true" ]]; then
  echo "Label 'safety-reviewed' present. Passing."
  exit 0
fi
echo "::error::This PR touches execution/broker/risk/security paths or removes tests. A human must review it and add the 'safety-reviewed' label."
exit 1
