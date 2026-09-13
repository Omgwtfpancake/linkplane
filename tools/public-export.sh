#!/bin/sh
# Produce the public-source snapshot: the tracked tree minus files that are private to the
# development repository. Never copies .git, untracked files, runtime state or build residue.
#
#   tools/public-export.sh [destination]      default: /tmp/linkplane-public-export
#
# Excluded on purpose (kept in the private repository):
#   docs/publication-readiness.md      the pre-publication audit; names the identifiers it found
#   docs/*.pdf, docs/phonebridge-progress-*.html   generated reports from the prototype era
set -eu
dest=${1:-/tmp/linkplane-public-export}
root=$(cd "$(dirname "$0")/.." && pwd)
rm -rf "$dest"
mkdir -p "$dest"
cd "$root"
git ls-files -z \
  | grep -zvE '^docs/publication-readiness\.md$|^docs/[^/]+\.pdf$|^docs/phonebridge-progress-.*\.html$' \
  | while IFS= read -r -d '' file; do
      mkdir -p "$dest/$(dirname "$file")"
      cp -p "$file" "$dest/$file"
    done
printf 'exported %s files to %s\n' "$(find "$dest" -type f | wc -l)" "$dest"
