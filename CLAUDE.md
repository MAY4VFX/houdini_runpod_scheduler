# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RunPod scheduler for SideFX Houdini PDG (Procedural Dependency Graph), based on the AWS ECS Scheduler from a SideFX content library example (MIT license, 2024). The original implementation dispatches PDG work items to AWS ECS; this project adapts it for RunPod.

## Repository Structure

- `AWSECS/awsecsscheduler.hda/` — The main HDA (Houdini Digital Asset) containing the scheduler. This is an expanded HDA directory; Python code lives in `Top_1awsecsscheduler/PythonModule` (702 lines) and UI definition in `DialogScript` (515 lines).
- `AWSECS/awsecs_example.hip` — Example Houdini scene with a TOP network using the scheduler.
- `AWSECS/container/Dockerfile` — Docker image: Ubuntu 20.04, headless Houdini, boto3, Intel OpenCL, xvfb. Requires `houdini*.tar.gz` installer placed in `container/` before building.
- `AWSECS/container/run.sh` — Entrypoint: sources Houdini env, executes `$COMMAND`.
- `AWSECS/backup/` — Backup of the original HDA.
- `AWS ECS Scheduler.html` — Help documentation (exported HTML).

## Working with the HDA Code

The HDA is in expanded directory format. Key editable files:

```
AWSECS/awsecsscheduler.hda/Top_1awsecsscheduler/
├── PythonModule      # Core scheduler Python code (edit directly)
├── DialogScript      # Parameter UI definition
├── Help              # Help card content
├── CreateScript      # Node creation script
└── Tools.shelf       # Shelf tool definition
```

To read/edit the scheduler logic, work with `PythonModule` directly. No need to use `strings` or Houdini Type Properties.

## Architecture

`AWSECSScheduler` class hierarchy:
- `PyScheduler` — Base PDG scheduler
- `SimpleMQSchedulerMixin` — Message queue polling for work item results (`startPollingClient`/`stopPollingClient`)
- `EventDispatchMixin` — Event handling

Lifecycle: `onStartCook` (validate params, create boto3 client) → `onSetupCook` (init working dir on EFS, copy job files, start MQ) → `onSchedule` (serialize work item, expand `__PDG_*__` tokens, call `_runTask` via boto3) → `onTick` (poll task status every 2s via `_describeTasks`) → `onStopCook` (stop tasks, stop MQ)

`submitAsJob` submits entire TOP graph cook as a single task.

ECS tasks receive commands via the `COMMAND` environment variable, which `run.sh` executes.

## Dependencies

- `boto3` — AWS SDK (install: `hython -m pip install boto3`)
- Houdini PDG framework: `pdg`, `pdg.scheduler`, `pdg.job.eventdispatch`, `pdg.utils.simple_mq`

## Docker Build

```bash
cd AWSECS/container
# Place houdini*.tar.gz installer first
docker build --build-arg EULA_DATE=yyyy-mm-dd --build-arg SESINETD=hostname:1715 -t aws-ecs-scheduler .
```

## Manual Testing

No automated tests. Test by opening `awsecs_example.hip`, configuring the scheduler parameters, and cooking `ropgeometry1` in `obj/topnet1`.

## Key Parameters (awsecs_ prefix)

`regionname`, `taskdefinition`, `cluster`, `containername`, `platformversion`, `subnet`, `securitygroup`, `launchtype` (EC2/FARGATE/EXTERNAL), `assignpublicip`, `verbose`, `overrideremoteworkingdir`, `remoteworkingdir`, `pretaskcmd`, `posttaskcmd`
