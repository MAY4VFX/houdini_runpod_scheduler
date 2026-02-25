#!/bin/bash
# Deploy RunPodFarm server to Dokploy
# Requires: DOKPLOY_URL and DOKPLOY_API_KEY environment variables
#
# Usage:
#   export DOKPLOY_URL=http://192.168.2.140:3001
#   export DOKPLOY_API_KEY=XdVofMdOfAlneojMFpBWplFeYWbxFzcUpuPBlQLYuBxmfWmjARKNyXwDEnsgMrZc
#   ./infrastructure/deploy-dokploy.sh
#
# Or just push to the correct branch (auto-deploy is configured in Dokploy).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DOKPLOY_URL="${DOKPLOY_URL:?Set DOKPLOY_URL (e.g. http://192.168.2.140:3001)}"
DOKPLOY_API_KEY="${DOKPLOY_API_KEY:?Set DOKPLOY_API_KEY}"

echo "=== RunPodFarm Deployment ==="
echo "Project root: $PROJECT_ROOT"
echo "Dokploy URL: $DOKPLOY_URL"
echo ""

# Step 1: Build dashboard
echo "[1/4] Building dashboard..."
cd "$PROJECT_ROOT/dashboard"
npm ci
npm run build
echo "Dashboard built successfully."

# Step 2: Copy dashboard dist to server/public
echo ""
echo "[2/4] Copying dashboard to server/public..."
rm -rf "$PROJECT_ROOT/server/public"
cp -r "$PROJECT_ROOT/dashboard/dist" "$PROJECT_ROOT/server/public"
echo "Dashboard copied to server/public."

# Step 3: Build server
echo ""
echo "[3/4] Building server..."
cd "$PROJECT_ROOT/server"
npm ci
npm run build
echo "Server built successfully."

# Step 4: Deploy via Dokploy API
echo ""
echo "[4/4] Triggering deployment on Dokploy..."

# Find the application ID first
APP_ID=$(curl -s -X GET "${DOKPLOY_URL}/api/application.all" \
  -H "x-api-key: ${DOKPLOY_API_KEY}" \
  -H "Content-Type: application/json" | \
  python3 -c "
import json, sys
apps = json.load(sys.stdin)
for app in apps:
    if 'runpodfarm' in app.get('name', '').lower():
        print(app['applicationId'])
        break
" 2>/dev/null || echo "")

if [ -z "$APP_ID" ]; then
  echo "WARNING: Could not find RunPodFarm application in Dokploy."
  echo "Make sure the application is created in Dokploy first."
  echo ""
  echo "To deploy manually:"
  echo "  1. Create an application in Dokploy named 'runpodfarm-server'"
  echo "  2. Configure it to build from this repository's 'server/' directory"
  echo "  3. Set environment variables: JWT_SECRET, DATABASE_PATH, REDIS_URL, CORS_ORIGIN"
  echo "  4. Push to the configured branch to trigger auto-deploy"
  exit 1
fi

echo "Found application: $APP_ID"

curl -s -X POST "${DOKPLOY_URL}/api/application.deploy" \
  -H "x-api-key: ${DOKPLOY_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"applicationId\": \"${APP_ID}\"}" | python3 -m json.tool 2>/dev/null || true

echo ""
echo "=== Deployment triggered! ==="
echo "Check status at: ${DOKPLOY_URL}"
