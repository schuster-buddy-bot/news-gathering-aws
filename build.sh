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

cp lambda_handler.py pdf_generator.py embeddings.py source_authority.py build/pipeline/
cp api_handler.py search_handler.py demo_handler.py embeddings.py source_authority.py build/api/

# API Lambda needs a recent boto3/botocore — the Lambda runtime's built-in
# version (~1.34) has a presigned-URL bug that truncates the SigV4 service
# name to "s" instead of "s3", producing broken download links.
# We bundle the same pinned versions used by the pipeline.
echo "Installing dependencies for Python ${PYV} (${PLATFORM})..."

PIP_INSTALL="python3 -m pip install --python-version ${PYV} --platform ${PLATFORM} --implementation cp --only-binary=:all: --upgrade --quiet"

if ! ${PIP_INSTALL} --target build/pipeline -r requirements.txt 2>/dev/null; then
    ${PIP_INSTALL} --break-system-packages --target build/pipeline -r requirements.txt
fi

# Install boto3+botocore into api build too (fixes presigned URL bug)
API_DEPS="boto3==1.43.91"
if ! ${PIP_INSTALL} --target build/api ${API_DEPS} 2>/dev/null; then
    ${PIP_INSTALL} --break-system-packages --target build/api ${API_DEPS}
fi

echo "Build complete:"
find build -maxdepth 2 -type d | sort
du -sh build/pipeline build/api