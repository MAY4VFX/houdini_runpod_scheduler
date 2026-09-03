"""Journal writer/reader, billing merge, and cost summaries for RunPodFarm.

Replaces the ``rpfarm.dispatch.append_record`` stand-in the scheduler falls
back to when this module can't be imported (see ``_ledger_append`` in
``hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule``).
``append(path, **record)`` is signature-compatible with that stand-in, so
the scheduler picks this module up with no code changes on its side.

Real record shapes, taken from the two places the scheduler actually
writes (not the task brief, which drifted from what shipped):

- task record, one per work item, from ``_poll_tasks``::

    {cook_id, user, project, work_item, work_item_name, pod, gpu,
     started, ended, duration_s, exit_code, cost_est}

- cook summary record, one per cook, from ``onStopCook``::

    {record: "cook_summary", cook_id, user, project, started, ended,
     canceled, items_failed, cost_est}

``cost_est`` on both is the scheduler's own live estimate (pod tariff x
duration, folding in this cook's share of pod boot/idle time for the
summary row). ``merge_billing`` below replaces it with RunPod's actual
billed ``cost`` once ``GET /billing/pods`` data is available -- the
estimate and the bill can differ (RunPod bills to the second, rounds up,
etc).
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path

# rpfarm-<user>-<project>-<cook8>-<n>  (project may itself contain dashes,
# so the cook_id -- always 8 hex chars -- and the trailing numeric index
# anchor the parse from the right; see rppods pod naming).
_POD_NAME_RE = re.compile(r"^rpfarm-([^-]+)-(.+)-([0-9a-f]{8})-(\d+)$")


def append(path, **record) -> None:
    """Append one JSON object as a line to *path*, creating parent dirs.

    Never raises OSError from a missing parent directory -- everything
    else (a full disk, a permissions error) is the caller's problem, same
    as the stand-in it replaces.
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def append_cook_summary(path, cook_id, user, project, pods=None, **extra) -> None:
    """Append a ``cook_summary`` record.

    The scheduler builds this shape inline in ``onStopCook`` (it already
    has a running ``cost_tracker``, see ``rpfarm.ledger``'s module
    docstring) and never calls this function -- it exists for callers that
    only have pod bookkeeping, such as the CLI's ``rpfarm costs`` or a
    test: pass ``pods`` (list of dicts with ``pod_id, gpu, cost_per_hr,
    created, terminated`` -- epoch seconds) and ``cost_est`` is derived as
    sum((terminated - created) / 3600 * cost_per_hr). Any of the real
    fields (``started, ended, canceled, items_failed, cost_est``) passed
    via ``**extra`` win over the derived value.
    """
    record = {"record": "cook_summary", "cook_id": cook_id, "user": user, "project": project}
    if pods:
        cost = 0.0
        for p in pods:
            created = p.get("created")
            terminated = p.get("terminated")
            if created is not None and terminated is not None:
                cost += (terminated - created) / 3600.0 * p.get("cost_per_hr", 0.0)
        record["cost_est"] = cost
    record.update(extra)
    append(path, **record)


def load_all(local_dir) -> list[dict]:
    """Load every record from every ``*.jsonl`` file under *local_dir*.

    Returns records in file-then-append order. Includes ``cook_summary``
    rows unchanged -- callers that only want task records should skip
    ``rec.get("record") == "cook_summary"`` themselves (``merge_billing``
    and ``summarize`` both do).
    """
    local_dir = Path(local_dir)
    records: list[dict] = []
    if not local_dir.is_dir():
        return records
    for p in sorted(local_dir.glob("**/*.jsonl")):
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    records.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _parse_pod_name(name):
    """``rpfarm-<user>-<project>-<cook8>-<n>`` -> ``(user, project, cook_id)``,
    or ``(None, None, None)`` if *name* doesn't match the farm convention
    (a sync pod, or another tenant's pod visible in the same billing call)."""
    m = _POD_NAME_RE.match(name or "")
    if not m:
        return None, None, None
    user, project, cook_id, _n = m.groups()
    return user, project, cook_id


def merge_billing(records, billing_pods) -> list[dict]:
    """Merge per-task ledger *records* with RunPod's actual ``/billing/pods``
    cost.

    Groups task records (``record != "cook_summary"``, has a ``pod``) by
    pod, matches each group to its ``billing_pods`` entry by ``podId``, and
    prorates that entry's ``amount`` across the group's records by
    ``duration_s / timeBilledSeconds`` -- the pod's billed time almost
    always exceeds the sum of its task durations (boot, idle between
    tasks), and that remainder becomes one synthetic ``kind="idle"``
    record per pod so the group's total ``cost`` always reconciles to the
    bill.

    Billing entries whose ``podName`` doesn't start with ``rpfarm-`` are
    someone else's pod on the same RunPod account (billing sees the whole
    account) and are ignored. A billed ``rpfarm-`` pod with no local task
    records at all (an orphan, the sync pod, or another machine's cook not
    yet synced) still becomes a standalone idle record, attributed via
    ``_parse_pod_name``.

    Records with no matching billing entry (pod still running, or outside
    the billing call's date range) pass through unmodified, still carrying
    ``cost_est`` instead of ``cost``. ``cook_summary`` records always pass
    through unmodified -- they have no ``pod``/``duration_s`` to prorate.
    """
    by_pod: dict[str, list[dict]] = {}
    for rec in records:
        if rec.get("record") == "cook_summary":
            continue
        pod = rec.get("pod")
        if pod is None:
            continue
        by_pod.setdefault(pod, []).append(rec)

    billed_by_pod = {
        b.get("podId"): b for b in billing_pods if (b.get("podName") or "").startswith("rpfarm-")
    }

    out: list[dict] = []
    for pod, recs in by_pod.items():
        billing = billed_by_pod.get(pod)
        if billing is None:
            out.extend(recs)
            continue

        billed_seconds = float(billing.get("timeBilledSeconds") or 0.0)
        amount = float(billing.get("amount") or 0.0)
        task_seconds = 0.0
        for rec in recs:
            duration = float(rec.get("duration_s") or 0.0)
            task_seconds += duration
            merged = dict(rec)
            merged["cost"] = (duration / billed_seconds * amount) if billed_seconds else 0.0
            merged["kind"] = "task"
            out.append(merged)

        idle_seconds = max(billed_seconds - task_seconds, 0.0)
        idle_cost = max(amount - (task_seconds / billed_seconds * amount if billed_seconds else 0.0), 0.0)
        sample = recs[0]
        out.append({
            "cook_id": sample.get("cook_id"),
            "user": sample.get("user"),
            "project": sample.get("project"),
            "pod": pod,
            "kind": "idle",
            "duration_s": idle_seconds,
            "cost": idle_cost,
        })

    for pod_id, billing in billed_by_pod.items():
        if pod_id in by_pod:
            continue
        user, project, cook_id = _parse_pod_name(billing.get("podName"))
        out.append({
            "cook_id": cook_id,
            "user": user or "unknown",
            "project": project or "unknown",
            "pod": pod_id,
            "kind": "idle",
            "duration_s": float(billing.get("timeBilledSeconds") or 0.0),
            "cost": float(billing.get("amount") or 0.0),
        })

    out.extend(rec for rec in records if rec.get("record") == "cook_summary")
    return out


def summarize(records, by="project", since=None, until=None) -> list[dict]:
    """Aggregate *records* (as returned by ``merge_billing``, or raw task
    records still carrying ``cost_est``) into totals grouped ``by``
    ``"project"``, ``"user"``, or ``"cook"``.

    Each result is ``{key, cost, gpu_hours, tasks, cost_per_task}``, sorted
    by ``cost`` descending. ``cost`` sums every record's ``cost`` (falling
    back to ``cost_est`` when billing wasn't merged in) including idle
    records, since idle time is real spend attributable to the group.
    ``gpu_hours`` likewise sums ``duration_s`` over every record (task +
    idle) -- it is pod-occupancy time, not just work-item time. ``tasks``
    counts records with ``kind in (None, "task")`` -- i.e. everything
    except a synthetic idle record.

    ``cook_summary`` rows are always excluded: their ``cost_est`` already
    covers the whole cook and would double-count against the per-task
    records that make up that same cook.

    ``since``/``until``, when given, are epoch seconds filtering on a
    record's ``started`` (falling back to ``ended``); a record with
    neither (e.g. a synthetic idle record) is always kept.
    """
    key_field = {"project": "project", "user": "user", "cook": "cook_id"}[by]
    groups: dict[str, dict] = {}
    for rec in records:
        if rec.get("record") == "cook_summary":
            continue
        ts = rec.get("started", rec.get("ended"))
        if ts is not None:
            if since is not None and ts < since:
                continue
            if until is not None and ts > until:
                continue
        key = rec.get(key_field) or "(unknown)"
        g = groups.setdefault(key, {"key": key, "cost": 0.0, "gpu_hours": 0.0, "tasks": 0})
        g["cost"] += rec.get("cost", rec.get("cost_est", 0.0)) or 0.0
        g["gpu_hours"] += (rec.get("duration_s") or 0.0) / 3600.0
        if rec.get("kind", "task") != "idle":
            g["tasks"] += 1

    out = list(groups.values())
    for g in out:
        g["cost_per_task"] = g["cost"] / g["tasks"] if g["tasks"] else 0.0
    out.sort(key=lambda g: -g["cost"])
    return out


def to_csv(records, path) -> None:
    """Write *records* (a list of flat dicts) to *path* as CSV.

    The column set is the union of every key seen across all records, in
    first-seen order -- a mixed task/idle/cook_summary list gets one
    ragged-but-readable table instead of raising on a KeyError.
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec:
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)


def sync_from_volume(client, local_dir, user) -> int:
    """Pull ``.jsonl`` ledger files from the volume that aren't present
    locally yet, via the worker HTTP API (``exec`` + ``read_file``) rather
    than rclone/sftp -- ``runpodfarm_stats`` already holds a
    :class:`~rpfarm.worker_client.WorkerClient` to the sync pod for
    billing, so this avoids a second sftp round trip to fetch a handful of
    small text files.

    Mirrors the volume's ``<user>/<cook_id>.jsonl`` layout under
    *local_dir*. Pulls every user's records, not just *user*'s -- stats
    aggregate across the whole account, and the point of syncing is to see
    "чужие" cooks too. A remote path with no ``<user>/`` component (the
    scheduler doesn't mirror its own journal up yet -- see Task 8 Step 14
    in the design doc) is bucketed under *user*'s own local directory
    rather than dropped.

    Returns the number of files newly written locally; a file that already
    exists locally is left untouched (the local copy, written directly by
    the scheduler, is the source of truth for records this machine wrote).
    """
    local_dir = Path(local_dir)
    result = client.exec("find /workspace/ledger -name '*.jsonl'", timeout_s=30)
    if result.get("exit_code") != 0:
        return 0

    new_count = 0
    for remote_path in (result.get("stdout") or "").splitlines():
        remote_path = remote_path.strip()
        if not remote_path:
            continue
        if remote_path.startswith("/workspace/ledger/"):
            rel = remote_path[len("/workspace/ledger/"):]
        else:
            rel = os.path.basename(remote_path)
        if "/" not in rel:
            rel = "{}/{}".format(user, rel)

        local_path = local_dir / rel
        if local_path.exists():
            continue
        content = client.read_file(remote_path)
        if content is None:
            continue
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content)
        new_count += 1
    return new_count
