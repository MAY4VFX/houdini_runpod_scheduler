# Unified Houdini flow: integration verification

Implementation branch: `ux/unified-upload-flow`; base `b93ce1c`.
The separate main checkout and its in-progress R71 edits were not modified.

## Implemented

- One file-review window: local references/farm tree, comparison states,
  selection, confirmed deletion, package grouping, preview versus pre-cook actions.
- Native transfer percentage/state and byte, speed, ETA and package attributes.
- Shared farm configuration across scheduler, transfer processes and management.
- Shared delivery receipts and process locks; asynchronous legacy delivery;
  native local PDG Output Files. Output-only auto-clean targets verified files
  from this cook, not entire output directories containing other cooks' results.
- Immediate readiness authorization errors; separate cook/job/farm status;
  durable job identity, cooperative cancellation, background status I/O.
- Explicit job target, render-only host controller, native checkpoint plus output
  manifest, and a Download Switch branch excluding upstream for a selected job.
- One shared MQ owned by the sync pod, not terminated when one cook finishes.
- Matching versioned Python/HDA installation, activated at the next launch.

## Verification completed locally

| Check | Result |
| --- | --- |
| Full pytest suite | 1237 passed, 2 skipped, 81.42 seconds |
| Final HDA/scheduler/release tests after tab visibility fix | 225 passed |
| Client, host, Download in three separate Houdini 22.0.368 processes | PASS: one Upload, one Render, one file copy, native local Output File |
| Second ordinary Cook Download | PASS: no upstream replay, no second file copy |
| Controller failure and pre-cook cancellation | PASS: failed/canceled terminal state, no task execution |
| HDA look/config/parameter checks | PASS, including hidden legacy background tab |
| Versioned installer into disposable preferences | PASS; fresh process creates all four nodes and constructs scheduler |
| Existing committed smoke HIP, no cook/save | PASS: Project and pod limit preserved, new controls available |
| File Review offscreen Qt | PASS: two trees, selection, Save Selection, screenshot inspected |
| Native transfer progress probe | PASS: observed 50%/Transferring during cook, 100% at completion |
| Whitespace and accidental credential scan | PASS |

Scripts: `verify_job_roundtrip.py`, `verify_controller_outcomes.py`,
`verify_hda_look.py`, `node_creation_smoke.py`, `verify_file_review_ui.py`,
`verify_transfer_progress_pdg.py` under `scripts/`.

The three-process test substitutes RunPod/worker/rclone transport with disposable
local fixture transport. It exercises native HDA generation, checkpoint loading,
host control, manifests, package execution, receipts and PDG output registration;
it does not prove cloud networking, licenses, container startup or termination.

## Release gates still open

- A separately authorized, cost-limited live RunPod round trip, including actual
  host/render self-termination and failure/cancellation behavior.
- Build/publish the new host image and coordinate activation of the template.
- Promote the PR and install the final bundle in the user's real preferences.
  Only disposable preferences were changed during this verification.
- Interactive inspection in the artist's real scene. Offscreen QA and the saved
  smoke fixture do not replace this check.

No paid pods were allocated by these checks, no user files or farm data were
deleted, and no active Houdini code or installed HDA files were replaced.
