#!/usr/bin/env bash
# Example fixed diagnostic script (admin-maintained, lives OUTSIDE the
# business repository at /etc/drawbridge/scripts/check_project_config.sh).
# Executed by the Runner as: bash --noprofile --norc <this file>
# cwd = the app's registered diagnostics root. No request argv is passed.
set -u

fail=0
for f in config/*.json; do
    [ -e "$f" ] || continue
    if python3 -m json.tool "$f" >/dev/null 2>&1; then
        echo "OK   $f"
    else
        echo "BAD  $f"
        fail=1
    fi
done
exit "$fail"
