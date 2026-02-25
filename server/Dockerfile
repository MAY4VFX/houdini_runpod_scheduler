# Multi-stage build: Dashboard + Server
# Build context should be repo root: docker build -f server/Dockerfile .

# --- Stage 1: Build Dashboard ---
FROM node:20-alpine AS dashboard-builder
WORKDIR /build
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# --- Stage 2: Build Server ---
FROM node:20-alpine AS server-builder
WORKDIR /build

# Install build tools for native modules (better-sqlite3)
RUN apk add --no-cache python3 make g++

COPY server/package*.json ./
RUN npm ci
COPY server/tsconfig.json ./
COPY server/src/ ./src/
RUN npm run build

# --- Stage 3: Production ---
FROM node:20-alpine
WORKDIR /app

# Install build tools for native modules rebuild
RUN apk add --no-cache python3 make g++

COPY server/package*.json ./
RUN npm ci --omit=dev && apk del python3 make g++

# Copy compiled server
COPY --from=server-builder /build/dist/ ./dist/

# Copy dashboard build as static files
COPY --from=dashboard-builder /build/dist/ ./public/

# Create data directory for SQLite
RUN mkdir -p /app/data

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3000/health || exit 1

CMD ["node", "dist/index.js"]
