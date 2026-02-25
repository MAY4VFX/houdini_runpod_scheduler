#!/bin/bash
# RunPodFarm - JuiceFS Setup Guide
#
# Prerequisites:
#   - JuiceFS CLI installed: curl -sSL https://d.juicefs.com/install | sh
#   - Upstash Redis URL (from setup-redis.sh)
#   - Backblaze B2 bucket + application key
#
# Backblaze B2 Setup:
#   1. Go to https://www.backblaze.com/cloud-storage
#   2. Create bucket: runpodfarm-{project_name} (EU Central, private)
#   3. Create Application Key (limited to this bucket)
#   4. Note: Endpoint URL, Key ID, Application Key

set -euo pipefail

echo "RunPodFarm JuiceFS Setup"
echo "========================"

# Required environment variables
: "${REDIS_URL:?Set REDIS_URL to Upstash Redis connection string}"
: "${B2_ENDPOINT:?Set B2_ENDPOINT (e.g., https://s3.eu-central-003.backblazeb2.com)}"
: "${B2_ACCESS_KEY:?Set B2_ACCESS_KEY to B2 Application Key ID}"
: "${B2_SECRET_KEY:?Set B2_SECRET_KEY to B2 Application Key}"
: "${B2_BUCKET:?Set B2_BUCKET to B2 bucket name}"

VOLUME_NAME="${VOLUME_NAME:-runpodfarm}"

echo "Formatting JuiceFS volume: $VOLUME_NAME"
echo "  Redis: ${REDIS_URL%%@*}@***"
echo "  S3: ${B2_ENDPOINT}/${B2_BUCKET}"

juicefs format \
    --storage s3 \
    --bucket "${B2_ENDPOINT}/${B2_BUCKET}" \
    --access-key "$B2_ACCESS_KEY" \
    --secret-key "$B2_SECRET_KEY" \
    --encrypt-rsa-key <(openssl genrsa 2048 2>/dev/null) \
    --compress lz4 \
    --trash-days 7 \
    "$REDIS_URL" \
    "$VOLUME_NAME"

echo ""
echo "JuiceFS volume formatted successfully!"
echo ""
echo "IMPORTANT: Save the RSA private key! It was generated during format."
echo "Export it with: juicefs config $REDIS_URL --encrypt-rsa-key"
echo ""
echo "To mount locally:"
echo "  juicefs mount $REDIS_URL /project --cache-size 51200"
