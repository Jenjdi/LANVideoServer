#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="${1:-$project_dir/build-release}"
build_type="${BUILD_TYPE:-Release}"
cmake_bin="${CMAKE:-cmake}"
ctest_bin="$(dirname "$(command -v "$cmake_bin")")/ctest"
python_bin="${PYTHON:-python3}"
deps_prefix="${LOCAL_DEPS:-/usr}"
if [[ -z "${LOCAL_DEPS:-}" && -d /workspace/toolchain/sysroot/usr ]]; then
    deps_prefix=/workspace/toolchain/sysroot/usr
fi
"$cmake_bin" -S "$project_dir" -B "$build_dir" -DCMAKE_BUILD_TYPE="$build_type" \
    -DLOCAL_DEPS="$deps_prefix" -DPython3_EXECUTABLE="$(command -v "$python_bin")"
"$cmake_bin" --build "$build_dir" --parallel "${JOBS:-2}"
"$ctest_bin" --test-dir "$build_dir" --output-on-failure
