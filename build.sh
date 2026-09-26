#!/usr/bin/env bash
# build.sh — prepare Lambda deployment packages for Terraform's archive_file.
#
#   build/pipeline/  lambda_handler.py + pdf_generator.py + pip deps
#   build/api/       api_handler.py (stdlib + boto3 only)
#
# Deps are installed pinned to the Lambda runtime (Python 3.12, manylinux2014)
# so no platform-incompatible wheels sneak in.
set -euo pipefail
cd "$(dirname "$0")"

PYV=3.12
PLATFORM=manylinux2014_x86_64

rm -rf build
mkdir -p build/pipeline build/api

cp lambda_handler.py pdf_generator.py embeddings.py build/pipeline/
cp api_handler.py embeddings.py build/api/

echo "Installing dependencies for Python ${PYV} (${PLATFORM})..."
if ! python3 -m pip install \
    --target build/pipeline \
    --python-version "${PYV}" \
    --platform "${PLATFORM}" \
    --implementation cp \
    --only-binary=:all: \
    --upgrade \
    --quiet \
    -r requirements.txt 2>/dev/null; then
    # Older pip or externally-managed env: retry with compatibility flags
    python3 -m pip install \
        --target build/pipeline \
        --python-version "${PYV}" \
        --platform "${PLATFORM}" \
        --implementation cp \
        --only-binary=:all: \
        --upgrade \
        --quiet \
        --break-system-packages \
        -r requirements.txt
fi

echo "Build complete:"
find build -maxdepth 2 -type d | sort
du -sh build/pipeline build/api