#!/usr/bin/env bash
# Builds dist/localcode-<version>-linux-<x64|arm64>.tar.gz for the machine it runs on:
# static launcher + the python-build-standalone CPython (glibc 2.17 baseline) + localcode.py.
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(grep -m1 '^__version__ = ' localcode.py | cut -d'"' -f2)
pbs=20261009
py=3.11.17
case "$(uname -m)" in
  x86_64)        arch=x86_64;  out=x64;   sha=035b0ccfe7f3d38ab3ddab505290d200c6a265b95fbd5fac76c358751d6bb6a8 ;;
  aarch64|arm64) arch=aarch64; out=arm64; sha=693ef5c3064e13af98178fba648c4757be92b842546a0ee2dae19ce1e3d863ba ;;
  *) echo "unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac
name=localcode-$version-linux-$out
file=cpython-$py+$pbs-$arch-unknown-linux-gnu-install_only.tar.gz

mkdir -p build/cache dist
if [ ! -f "build/cache/$file" ]; then
  curl -fsSL --retry 3 -o "build/cache/$file" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$pbs/${file//+/%2B}"
fi
echo "$sha  build/cache/$file" | sha256sum -c -

rm -rf build/stage
mkdir -p build/stage
tar -xzf "build/cache/$file" -C build/stage
mv build/stage/python build/stage/runtime
mkdir build/stage/localcode
mv build/stage/runtime build/stage/localcode/runtime
r=build/stage/localcode/runtime

# Drop what localcode never imports (tests, GUI, pip, headers, build config); keeps the download small.
pylib=$(echo "$r"/lib/python3.*)
rm -rf "$r"/include "$r"/share "$r"/bin/pip* "$r"/bin/idle* "$r"/bin/2to3* "$r"/bin/pydoc* \
       "$pylib"/test "$pylib"/idlelib "$pylib"/tkinter "$pylib"/turtledemo "$pylib"/ensurepip "$pylib"/lib2to3 \
       "$pylib"/config-* "$pylib"/site-packages/* "$pylib"/lib-dynload/_tkinter* \
       "$r"/lib/tcl* "$r"/lib/tk* "$r"/lib/itcl* "$r"/lib/thread* "$r"/lib/libtcl* "$r"/lib/libtk* "$r"/lib/pkgconfig
find "$r" -name __pycache__ -prune -exec rm -rf {} +

gcc -O2 -Wall -Werror -static -s -o build/stage/localcode/localcode launcher/launcher.c
cp localcode.py LICENSE README.md build/stage/localcode/

tar --owner=0 --group=0 -czf "dist/$name.tar.gz" -C build/stage localcode
ls -l "dist/$name.tar.gz"
