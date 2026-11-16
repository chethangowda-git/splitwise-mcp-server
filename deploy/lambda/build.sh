#!/bin/sh
# Build build/lambda/function.zip: the server plus its dependencies, as Linux arm64 wheels
# for the Lambda Python 3.13 runtime. No Docker needed.
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/build/lambda"
rm -rf "$out"
mkdir -p "$out/pkg"
"$root/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all: \
  --target "$out/pkg" -r "$root/requirements.txt"
cp -R "$root/src" "$root/main.py" "$out/pkg/"
cp "$root/deploy/lambda/run.sh" "$out/pkg/run.sh"
chmod 755 "$out/pkg/run.sh"
find "$out/pkg" -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd "$out/pkg" && zip -qr "$out/function.zip" .)
echo "Built $out/function.zip ($(du -h "$out/function.zip" | cut -f1))"
