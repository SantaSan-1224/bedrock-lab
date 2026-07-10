#!/usr/bin/env bash
# AgentCore Runtime 直接コードデプロイ用の .zip を作る (Phase 3 M3)。
#
# Runtime は arm64 / Python 3.13 で動くため、依存パッケージは
# uv の --python-platform 指定でクロスプラットフォーム取得する。
#
# 使い方:
#   scripts/build_runtime_zip.sh
#   → build/runtime/deployment_package.zip が生成される
#   → その後 terraform apply (aws_s3_object がこの zip を参照)
set -euo pipefail

cd "$(dirname "$0")/.."
WORK=build/runtime
rm -rf "$WORK"
mkdir -p "$WORK/pkg"

# 依存を arm64 / cp313 向けに取得 (Runtime のアーキテクチャに合わせる)
uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --target="$WORK/pkg" \
  --only-binary=:all: \
  bedrock-agentcore boto3

# zip ルート = import パス。依存 → エージェントコードの順に詰める
(cd "$WORK/pkg" && zip -qr ../deployment_package.zip .)
zip -qj "$WORK/deployment_package.zip" client/agent_cli.py client/agent_runtime.py

echo "created: $WORK/deployment_package.zip ($(du -h "$WORK/deployment_package.zip" | cut -f1))"
