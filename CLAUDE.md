# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

RunPodFarm — distributed VFX rendering/simulation pipeline on RunPod GPU Pods for SideFX Houdini. Based on the AWS ECS Scheduler from a SideFX content library example (MIT license, 2024), adapted for RunPod with Redis-based task queue, JuiceFS shared filesystem, and a full management stack.

## Architecture

PDG-native scheduling: no separate Scheduler Server. Each Houdini instance runs its own RunPodFarm Scheduler HDA which manages pods directly via RunPod API and distributes tasks via Redis queue.

```
Houdini (Scheduler HDA) → Redis queue → RunPod Pods (Worker daemon)
                                      ↕
Desktop App → Auth API → JuiceFS config → JuiceFS mount (B2 + Redis)
                                      ↕
Dashboard (Web UI) ← Monitoring API ← Redis (read-only)
```

## Repository Structure

```
worker/                    — Worker daemon (Python) running on RunPod pods
  config.py                  Config from env vars
  daemon.py                  Main loop: BRPOP tasks, execute, push results
  executor.py                Task execution (hython/husk subprocess)
  heartbeat.py               Heartbeat thread (Redis SET with TTL)
  requirements.txt           redis>=5.0.0, psutil>=5.9.0

hda/runpodfarm_scheduler.hda/  — Houdini Digital Asset (expanded format)
  Top_1runpodfarmscheduler/
    PythonModule             RunPodFarmScheduler class (1635 lines)
    DialogScript             Parameter UI definition (603 lines)
    CreateScript             Node creation script
    Help                     Help card (274 lines)
    Tools.shelf              Shelf tool definition

docker/                    — Docker image for RunPod pods
  Dockerfile                 Ubuntu 22.04 + CUDA 12.4 + JuiceFS + Worker
  entrypoint.sh              Mount JuiceFS → setup Houdini → start worker
  docker-compose.dev.yml     Local dev environment with Redis
  .dockerignore

auth-api/                  — Auth API (Cloudflare Workers + Hono + KV) [LEGACY]
  src/index.ts               Main entry, CORS, routing
  src/types.ts               TypeScript interfaces
  src/auth.ts                JWT, password hashing, API key generation
  src/routes/auth.ts         POST /auth/login, /auth/register
  src/routes/projects.ts     CRUD projects, artists, config endpoint
  wrangler.toml              Cloudflare Workers config

server/                    — Node.js server (Hono + SQLite) — replaces auth-api for Dokploy
  src/index.ts               Main entry, CORS, routing, static file serving
  src/types.ts               TypeScript interfaces
  src/db.ts                  SQLite storage layer (better-sqlite3)
  src/auth.ts                JWT, password hashing (Node.js crypto), API key generation
  src/routes/auth.ts         POST /api/auth/login, /api/auth/register
  src/routes/projects.ts     CRUD projects, artists, config endpoint
  src/routes/monitoring.ts   GET /api/monitoring/jobs|pods|costs|logs (Redis read-only)
  Dockerfile                 Production Docker image
  docker-compose.yml         Local dev with Docker

dashboard/                 — Monitoring Web UI (React + TypeScript + Vite + Tailwind)
  src/pages/Dashboard.tsx    Active jobs, pods, cost overview
  src/pages/Projects.tsx     Project management
  src/pages/ProjectDetail.tsx  Jobs, pods, artists per project
  src/pages/Login.tsx        Authentication
  src/components/Layout.tsx  Sidebar navigation
  src/lib/api.ts             API client
  src/store/auth.ts          Zustand auth store

desktop-app/               — Desktop App (Tauri 2.0 + React + TypeScript)
  src-tauri/src/main.rs      Rust backend: JuiceFS mount, system tray, Houdini detection
  src-tauri/tauri.conf.json  App configuration
  src/App.tsx                Connect/Status/Settings views
  src/components/            ConnectForm, StatusPanel, Settings
  src/lib/tauri.ts           Typed Tauri command wrappers

infrastructure/            — Setup scripts and configs
  setup-redis.sh             Upstash Redis provisioning guide
  setup-juicefs.sh           JuiceFS format with B2 backend
  example.env                Template for all env vars

AWSECS/                    — Original AWS ECS Scheduler (reference)
```

## Key Commands

```bash
# Worker (local dev)
cd docker && docker compose -f docker-compose.dev.yml up

# Auth API (legacy CF Workers)
cd auth-api && npm install && npm run dev    # Local dev
cd auth-api && npm run deploy                # Deploy to CF Workers

# Server (Node.js — for Dokploy deployment)
cd server && npm install && npm run dev      # Local dev (tsx watch)
cd server && npm run build && npm start      # Production build + start

# Dashboard
cd dashboard && npm install && npm run dev   # Dev server
cd dashboard && npm run build                # Production build

# Deploy to Dokploy
./infrastructure/deploy-dokploy.sh           # Full build + deploy

# Desktop App
cd desktop-app && npm install
cd desktop-app && cargo tauri dev            # Dev mode
cd desktop-app && cargo tauri build          # Production build

# Docker image
docker build -f docker/Dockerfile -t runpodfarm-worker .
```

## HDA Parameter Prefix

All scheduler parameters use `rpfarm_` prefix (e.g., `rpfarm_apikey`, `rpfarm_redisurl`).

## Redis Key Namespace

```
rp:tasks:{project_id}:{user_id}   — Task queue (BRPOP)
rp:results:{task_id}               — Task results (SET with TTL)
rp:heartbeat:{pod_id}              — Pod heartbeats (SET with 30s TTL)
rp:logs:{task_id}                  — Task logs (RPUSH with TTL)
rp:pods:{project_id}:{user_id}    — Pod registry
rp:metrics:*                       — Metrics and cost tracking
juicefs:*                          — JuiceFS metadata (managed by JuiceFS)
```

## Dependencies

- **Worker**: Python 3.10+, redis, psutil
- **HDA**: Houdini 20.0+ with PDG, redis (pip install to hython)
- **Auth API (legacy)**: Node.js 18+, hono, jose
- **Server**: Node.js 20+, hono, @hono/node-server, jose, better-sqlite3, ioredis
- **Dashboard**: Node.js 18+, React 18, Vite, Tailwind
- **Desktop App**: Rust 1.70+, Node.js 18+, Tauri 2.0

## Manual Testing

No automated tests yet. Test flow:
1. Infrastructure: `juicefs mount` locally → write file → mount on pod → file visible
2. Worker: start pod → worker connects to Redis → heartbeat visible → push test task → result in Redis
3. HDA: Houdini → TOP Network → RunPodFarm Scheduler → cook → frames render on pods → results in /project/renders/
### Dokploy Server
- **Host**: 192.168.2.140
- порт апи 3001
- **SSH Access**: `ssh -o StrictHostKeyChecking=no root@192.168.2.140`
- **Auto-deploy**: Enabled - пуш в правильную ветку автоматически запускает деплой
- **Dokploy API Key** x-api-key : XdVofMdOfAlneojMFpBWplFeYWbxFzcUpuPBlQLYuBxmfWmjARKNyXwDEnsgMrZc
- делай коммит  и пуш после правок
- никогда не собирать докер композы в ручную только через докплой и репозиторий
