#!/usr/bin/env bash
# Smoke-tests dist/localcode-*-linux-*.tar.gz the way a user with no Python would run it,
# then again inside an old-glibc Debian container when Docker is available.
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(grep -m1 '^__version__ = ' localcode.py | cut -d'"' -f2)
case "$(uname -m)" in x86_64) arch=x64 ;; *) arch=arm64 ;; esac
tarball=$(pwd)/dist/localcode-$version-linux-$arch.tar.gz
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work"
lc=$work/localcode/localcode
mkdir "$work/cwd with space"
cd "$work/cwd with space"
failures=0

check() { # check <description> <command...>
  local what=$1; shift
  if "$@"; then echo "ok   $what"; else echo "FAIL $what"; failures=$((failures + 1)); fi
}
# Run with an empty environment plus junk Python variables, so nothing but the bundled runtime can be used.
# Output and exit code land in $work/out.txt and $rc; stdin comes from $IN (default: empty).
run() {
  env -i HOME="$work" PATH=/usr/bin:/bin PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent "$@" \
    > "$work/out.txt" 2>&1 < "${IN:-/dev/null}" && rc=0 || rc=$?
}

run "$lc" --version
check "--version prints 'localcode $version'" test "$(cat "$work/out.txt")" = "localcode $version"
run "$lc" --help
check "--help lists the flags" grep -q -- --max-prompt "$work/out.txt"
run "$lc" --bogus
check "argparse exit code 2 passes through" test $rc = 2
run "$lc" --ctx 100 --max-tokens 200 --model x
check "exit code 1 passes through" test $rc = 1
check "arguments reach localcode" grep -q 'max-tokens must be smaller than ctx' "$work/out.txt"
printf 'hi\n/exit\n' > "$work/in.txt"
IN=$work/in.txt run "$lc" --model 'my model "quoted"' --url http://127.0.0.1:9/v1
check "an argument with spaces and quotes arrives intact" grep -qF 'my model "quoted"' "$work/out.txt"
check "with no server running, a prompt reports the failure instead of crashing" grep -q 'request failed' "$work/out.txt"
run "$lc" --url http://127.0.0.1:9/v1
check "with no server and no --model, it exits 1" test $rc = 1
check "... and says why" grep -q 'cannot list models' "$work/out.txt"
run "$work/localcode/runtime/bin/python3" -I -c 'import argparse, ast, ctypes, json, signal, ssl, subprocess, urllib.request'
check "the bundled Python has the modules localcode needs" test $rc = 0

if command -v docker >/dev/null 2>&1; then
  # Debian 11 has glibc 2.31 and no Python: proves the launcher and runtime need nothing from the host.
  docker run --rm -v "$work/localcode:/lc:ro" debian:11-slim /lc/localcode --version > "$work/out.txt" 2>&1 && rc=0 || rc=$?
  check "runs inside debian:11-slim (glibc 2.31, no Python installed)" test "$(cat "$work/out.txt")" = "localcode $version"
else
  echo "skip: debian:11-slim container test (no docker)"
fi

[ "$failures" = 0 ] || { echo "$failures smoke check(s) failed"; exit 1; }
echo "smoke tests passed"
