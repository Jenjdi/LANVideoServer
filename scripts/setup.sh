#!/usr/bin/env bash
set -euo pipefail
# Debian 13 cloud bootstrap; development packages are extracted without sudo.
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
tools_dir=/workspace/toolchain
mkdir -p "$tools_dir/apt/lists/partial" "$tools_dir/apt/cache/archives/partial" "$tools_dir/debs" "$tools_dir/sysroot"
cat > "$tools_dir/apt/apt.conf" <<'EOF'
Dir::Etc::parts "-";
Dir::Etc::main "-";
Dir::Etc::sourcelist "/workspace/toolchain/apt/sources.list";
Dir::Etc::sourceparts "-";
Dir::State "/workspace/toolchain/apt";
Dir::State::status "/var/lib/dpkg/status";
Dir::State::lists "/workspace/toolchain/apt/lists";
Dir::Cache "/workspace/toolchain/apt/cache";
APT::Get::List-Cleanup "false";
EOF
cat > "$tools_dir/apt/sources.list" <<'EOF'
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie main
EOF
export APT_CONFIG="$tools_dir/apt/apt.conf"
/usr/bin/apt-get update
(
  cd "$tools_dir/debs"
  /usr/bin/apt-get download nginx nginx-common libsqlite3-dev libsqlite3-0 libtorrent-rasterbar-dev libtorrent-rasterbar2.0t64 libcpp-httplib-dev libcpp-httplib0.18 nlohmann-json3-dev libsodium-dev libsodium23 libboost1.83-dev libboost-system1.83-dev libboost-system1.83.0
  for package in *.deb; do dpkg-deb -x "$package" "$tools_dir/sysroot"; done
)
if [ ! -x "$tools_dir/venv/bin/python" ]; then python3 -m venv "$tools_dir/venv"; fi
"$tools_dir/venv/bin/pip" install --no-cache-dir --disable-pip-version-check cmake==3.31.10 -r "$project_dir/tests/requirements.txt"
# Integration tests require the image's media and browser executables.
command -v ffmpeg >/dev/null || { echo "Tests require FFmpeg" >&2; exit 1; }
test -x /usr/bin/chromium || { echo "Browser tests require /usr/bin/chromium" >&2; exit 1; }
command -v openssl >/dev/null || { echo "TLS tests require OpenSSL command-line tools" >&2; exit 1; }
"$tools_dir/sysroot/usr/sbin/nginx" -v
CMAKE="$tools_dir/venv/bin/cmake" PYTHON="$tools_dir/venv/bin/python" \
    LOCAL_DEPS="$tools_dir/sysroot/usr" BUILD_TYPE=Debug bash "$project_dir/scripts/build.sh" "$project_dir/build"
