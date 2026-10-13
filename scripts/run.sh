#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
binary="${LANVIDEO_BINARY:-$project_dir/build-release/lanvideo}"
if [[ ! -x "$binary" && -z "${LANVIDEO_BINARY:-}" ]]; then binary="$project_dir/build/lanvideo"; fi
exec "$binary"
