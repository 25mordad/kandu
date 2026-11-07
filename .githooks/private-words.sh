# Shared by the hooks in this folder: they source it and pipe text through scan_private.
# The terms are in local/private-words.txt of the main working tree, so a linked
# worktree uses them too. One term per line, case-insensitive; blank lines and
# # comments are ignored. Text inside a compressed file or in UTF-16 is not seen.

# With the install's .venv, kandu/privateterms.py adds the terms of the setup itself
# (projects, repos, paths, machines, .env values), so a new project is covered at once.
# If it fails, every commit and push is blocked: the guard never passes what it could not read.

common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
root="$(dirname "$common_dir")"
words_file="$root/local/private-words.txt"

if [ -x "$root/.venv/bin/python" ] && [ -f "$root/kandu/privateterms.py" ]; then
  if ! patterns="$("$root/.venv/bin/python" -I "$root/kandu/privateterms.py" "$root/local" 2>/dev/null)"; then
    echo "private guard: could not read the private terms from $root/local" >&2
    exit 1
  fi
elif [ -f "$words_file" ]; then
  patterns="$(sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$words_file" | grep -vE '^(#|$)' || true)"
else
  patterns=""
fi
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
