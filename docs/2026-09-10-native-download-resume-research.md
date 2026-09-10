# Native Cook Download resume: Houdini 22.0.368

TL;DR: a native Switch TOP inside Download can exclude Upload/Render before PDG evaluates the graph.
An independent resume source restores saved graph state and imports a host-written output manifest, then feeds the existing Download processor.
Tested with a saved packed HDA and two separate hython processes; this needs no manual Restore action or Python class monkeypatch.

## Recommended topology

```text
Download subnet external input ── Switch input 0 ─┐
                                                ├─ Switch ─ existing Download processor ─ output0
Independent resume PythonProcessor ─ input 1 ───┘
```

The Switch selector reads the persisted job selection. An empty `rpfarm_job`
means Current Graph (input 0); a selected saved job means input 1. The expression
does not fetch remote data or mutate anything:

```python
switch.parm("input").setExpression(
    "1 if hou.pwd().parent().evalParm('rpfarm_job') else 0",
    language=hou.exprLanguage.Python,
)
```

SideFX specifies that the Switch evaluates selection before work-item generation
and excludes unselected branches from the cook. This is the property that prevents
an accidental farm resubmission. [Switch TOP](https://www.sidefx.com/docs/houdini/nodes/top/switch.html)

The independent resume source can use the stock PythonProcessor's generation
callback. Restore the known job's checkpoint, then create independent source
items from its output manifest, preserving output path/tag, original render item
identity, `rpfarm_pathmap`, and `delivery_key`. Do not make those imported items
children of the restored render items. The ordinary downstream Download processor
keeps its existing `AllUpstreamCooked` generation and transfer implementation.
No new custom PDG class or registration hook is required.

Wire `output0` to the common Download processor. Merely changing the subnet's
display flag leaves an existing Output TOP authoritative. That initially caused
the probe to bypass the Switch entirely. [TOP cooking and subnet outputs](https://www.sidefx.com/docs/houdini/tops/cooking.html)

## Verified evidence

Repro: [research-resume/switch_probe.py](research-resume/switch_probe.py).
The script uses only local in-process marker tasks, a temporary HDA, a HIP,
a checkpoint and an output manifest. It imports no RunPod code or credentials.

1. Process 1 creates `A upload → B dynamic render → Download subnet`, persists a
   selected job, packs the subnet as an HDA and saves the HIP before cooking.
2. It cooks only B, yielding execution markers `A, B`. The downstream resume
   source does not execute. B writes a remote EXR result plus path-map and delivery
   attributes during its cook. The process serializes graph work items and exports
   their final output data to a JSON manifest outside a generation callback.
3. Process 2 opens the saved HIP in a new hython process and calls only
   `Download.cookWorkItems(block=True, save_prompt=False)` twice.
4. The resume callback's cook set contains only the Download source, Switch and
   common processor. A/B each have exactly one restored `CookedSuccess` work item;
   their execution markers remain one apiece.
5. The common Download work item asserts the exact remote EXR input path and both
   metadata values. Its locally written marker is registered as an Output File.

Final saved-repro run on 22.0.368: both processes exited 0. Process 1 printed
`EXECUTIONS ['A', 'B']`; process 2 printed
`RESUME COOK SET ['Download_resume', 'Download_source', 'Download_download']`
and `EXECUTIONS ['A', 'B', 'DOWNLOAD', 'DOWNLOAD']`.

`cookWorkItems` is the standard HOM API corresponding to cooking the selected TOP.
[hou.TopNode](https://www.sidefx.com/docs/houdini/hom/hou/TopNode.html)

Example invocation (run the last two commands as separate processes):

```sh
probe_dir=$(mktemp -d /tmp/rpfarm-resume.XXXXXX)
export HOUDINI_USER_PREF_DIR="$probe_dir/prefs__HVER__"
export RPFARM_ROOT=/path/to/houdini_runpod_scheduler
probe_hython=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/22.0/Resources/bin/hython
"$probe_hython" docs/research-resume/switch_probe.py first "$probe_dir"
"$probe_hython" docs/research-resume/switch_probe.py second "$probe_dir"
```

The probe accepts re-generation of a Download item on the second cook. It proves
that Upload/Render do not repeat. The product's delivery receipts must suppress
repeat file copies and validate the already-downloaded local outputs; a generated
work item is not itself evidence that bytes were copied. The initial no-output
minimal probe produced `A, B, DOWNLOAD` across both cooks. Adding remote file
references made the simple fake Download regenerate; that is why this report does
not claim checkpoint restoration alone provides transfer deduplication.

## Alternatives tested and rejected

| Mechanism | Local result |
| --- | --- |
| Stock PythonProcessor `onPreCook` parameter | Not exposed in installed 22.0.368. |
| Custom `PyProcessor.onPreCook` + `context.deserializeWorkItems` | Restored static A/B before C generation and prevented reruns for the simple static graph. |
| Same hook with dynamic built-in PythonProcessor B | B generated an additional child and ran again. |
| Manual deserialization before cooking, diagnostic only | Same dynamic B duplication; therefore not solely an `onPreCook` ordering problem. |
| Native Python Load Checkpoint button, diagnostic only | Same dynamic B duplication in this probe. |
| Change checkpoint suffix from `.py` to `.bin` | No effect: `serializeWorkItems(path, '')` still writes Python source, and dynamic B still repeats. |
| Resume static generator reads restored dynamic B attrs/results | `outputFiles` was empty and dynamic string attrs returned `None`, although present in serialized data. |
| Wrap those reads in `restored.makeActive()` | Same empty/None values. |
| Set source to `AllUpstreamCooked` with no inputs | Did not expose restored dynamic attributes. |
| Switch + host output manifest + one common Download processor | Passed separate-process resume, metadata assertions and no A/B rerun; packed HDA loads without manual registration. |

The dynamic B used here is a stock PythonProcessor with
`requirescookedinputs=1` and an `onGenerate` script that creates a parented item.
The duplication is an observed limitation of this tested graph and legacy
checkpoint path, not a claim that every Houdini dynamic generator behaves this way.

SideFX documents `onPreCook` as running before generation on active nodes. A
SideFX staff answer specifically says the PythonProcessor parameter interface
does not expose it. [Processor callbacks](https://www.sidefx.com/docs/houdini/tops/processors.html),
[SideFX staff explanation](https://www.sidefx.com/forum/post/399136/).

Local API introspection found no documented `NodeOptions` setting that removes
connected inputs from a cook. `requiresGeneratedInputs` and
`requiresCookedInputs` control generation readiness; they are not graph-branch
exclusion. [NodeOptions](https://www.sidefx.com/docs/houdini/tops/pdg/NodeOptions.html)

## Boundaries for product integration

- Keep `rpfarm_job` selection stable through the cook and across reopening the
  scene. Missing or incomplete selected-job artifacts must fail the resume branch,
  not silently select Current Graph and submit again.
- Validate manifest/checkpoint identity against the selected job, target graph
  and completed host cook before importing anything.
- Export the manifest after the host cook, when dynamic results are visible.
  Include all required render outputs, tags, original delivery keys and path maps.
- This experiment uses the installed legacy `deserializeWorkItems` API. Its
  `pdg/context.py` implementation executes Python source from the checkpoint;
  `.bin` is not a security boundary. Only the application's own validated job
  artifacts belong on this path. The new JSON checkpoint mechanism exists in
  22.0.368 but was not evaluated by this bounded probe.
- For normal Cook Render / Submit targeting B, downstream Download is not in the
  cook set: the first-process test demonstrates this even with a job selected.
- The local verification does not exercise network fetch failures, pod shutdown,
  real ROP batch/subitem outputs, cancellation, or the UI's job-selection widget.
  Those require product-level integration checks.
