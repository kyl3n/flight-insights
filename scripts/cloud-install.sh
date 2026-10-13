#!/usr/bin/env bash
set -euo pipefail
cd /workspace/flight-insights

# This helper uses signed package indexes and verified Rust artifact hashes.
# It writes dependencies under /workspace/shared, not system directories.
python3 scripts/cloud-native-setup.py
source scripts/cloud-native-env.sh
export UV_CACHE_DIR=/workspace/shared/q22-uv-cache
export npm_config_cache=/workspace/shared/q22-npm-cache
export PYINSTALLER_CONFIG_DIR=/workspace/shared/q22-toolchain/pyinstaller-cache

uv sync --frozen
npm ci --no-fund --no-audit
uv run --frozen python scripts/package-engine.py
npm run build
cargo fetch --manifest-path src-tauri/Cargo.toml --locked
cargo fetch --manifest-path native-repair/Cargo.toml --locked
uv run --frozen python scripts/application-dependencies.py --target x86_64-unknown-linux-gnu
