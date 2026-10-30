# Shared by the hooks in this folder: they source it and pipe text through scan_private.
# The terms are in local/private-words.txt of the main working tree, so a linked
# worktree uses them too. One term per line, case-insensitive; blank lines and
# # comments are ignored. Text inside a compressed file or in UTF-16 is not seen.

common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
words_file="$(dirname "$common_dir")/local/private-words.txt"
[ -f "$words_file" ] || exit 0

patterns="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$words_file" | grep -vE '^(#|$)' || true)"
[ -n "$patterns" ] || exit 0

# Prints the lines of stdin that contain a private term.
scan_private() {
  tr -d '\0' | grep -aiF -f <(printf '%s\n' "$patterns") || true
}

added_lines() {
  grep -a '^+' || true
}

blocked() {
  echo "$1 blocked: private terms found." >&2
  printf '%s\n' "$2" >&2
  exit 1
}
