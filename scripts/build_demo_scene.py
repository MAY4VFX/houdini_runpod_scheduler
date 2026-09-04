"""Build the artist-facing demo scene: ``rpfarm_demo.hip`` + its folder.

This is the scene the farm's owner opens to decide whether the system works,
so it is deliberately *not* the smoke fixture. The smoke fixture is a grey
sphere at 320x240 that measures the farm; this one is meant to be looked at:
a lit pig head on a studio floor, 1280x720 Karma (CPU by default, see
``ENGINE``), eight frames that rotate
enough to read as a sequence.

Run it with Houdini's ``hython``::

    /Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/\\
Versions/22.0/Resources/bin/hython scripts/build_demo_scene.py \\
        [~/Desktop/rpfarm_demo]

Everything the artist has to do is one button. The design decisions behind
that, in the order they matter:

- **One button, one cook.** ``SUBMIT_TO_FARM`` calls ``cookWorkItems`` exactly
  once, on the downstream-most node (``download``), non-blocking. Cooking the
  ROP Fetch and then the download separately would start two independent PDG
  cooks and pay for a second GPU pod -- Task 10 found that live. The callback
  also refuses to start while a cook is already running, because a second
  click is the same mistake by a different route.
- **The button saves the scene first.** The farm renders the ``.hip`` on disk,
  not the one in RAM. Saving first is what every farm submitter does and it
  removes the one way an artist can be honestly confused about what rendered.
- **``waitforall`` between upload and render.** ``runpodfarm_upload`` emits one
  work item *per package*, and every node below generates per incoming item;
  with two packages (the scene's own files, and the referenced HDA libraries
  under ``_ext/``) the ROP Fetch would plan 2 x 8 items and render every frame
  twice, at double the money. The partitioner collapses the packages into one.
  Partition/merge items carry no command and never reach the scheduler, so the
  gate is free on the farm.
- **``$HIP`` in the ROP's output path, never ``$JOB``.** The pod's worker runs
  each task from the container's own working directory and nothing puts ``JOB``
  in the task environment, so ``$JOB`` inside a pod-side ``hython`` expands to
  that cwd. ``$HIP`` is always the loaded hip's directory, which on the pod is
  the mapped project directory.
- **``matchCurrentDefinition()`` before saving.** ``runpodfarm_upload`` and
  ``runpodfarm_download`` are subnets whose ``OnCreated`` unlocks their
  contents (Ruling R22), and an unlocked HDA instance saves its *whole internal
  network* into the ``.hip``. A scene saved today would otherwise go on running
  whatever generate script was installed the day it was built, ignoring every
  later HDA rebuild -- three live runs in Task 15 downloaded nothing for
  exactly this reason. ``_verify()`` asserts it stuck.
- **Karma CPU, by measurement.** The farm CAN run XPU now (the pod image was
  missing libEGL), but on this scene at these settings XPU is 1.4x slower --
  see ``ENGINE`` for the numbers and why. ``engine`` is set explicitly rather
  than left to the default so a future default change cannot quietly move this
  scene onto the other renderer.

The button code lives in the scene's Python source editor (``hou.session``),
so the ``.hip`` is genuinely self-contained -- no loose ``.py`` beside the
render output that can go missing.
"""

from __future__ import annotations

import math
import os
import sys

import hou

DEFAULT_DEST_DIR = os.path.expanduser("~/Desktop/rpfarm_demo")
HIP_NAME = "rpfarm_demo.hip"

# Eight frames: enough that the sequence reads as a sequence and that per-frame
# distribution across pods is visible in the ledger, few enough to stay cents.
FRAME_START = 1
FRAME_END = 8

RESX = 1280
RESY = 720
SAMPLES = 9

# "cpu" or "xpu". Selectable, and measured rather than assumed.
#
# The farm can run XPU (that was a missing libEGL in the pod image, fixed), but
# on this workload it loses: the same frame of this scene at 1280x720, spp 9,
# on an RTX 4090 pod with 32 vCPU took 35.7s on CPU and 50.5s on XPU. Two
# reasons, and both are structural rather than bad luck. RunPod's GPU pods come
# with a lot of CPU, which Karma CPU uses all of; and XPU compiles OptiX
# kernels at startup, which this farm pays on EVERY frame because it dispatches
# one frame per task in a fresh hython -- the cost never amortizes.
#
# Renting a GPU and rendering on its CPU still looks wrong, and it is worth
# revisiting: XPU wins on heavier scenes, more samples, higher resolution, or
# with several frames per task (ROP Node Configuration instead of Frame Range)
# so one compile serves many frames. Change ENGINE here and re-measure; do not
# switch it on the strength of "XPU works now".
ENGINE = "cpu"

CAM_FOCAL = 75.0        # long enough not to distort a face at this distance
CAM_AZIMUTH = 28.0      # degrees right of front -- a three-quarter view
CAM_ELEVATION = 13.0    # degrees above the aim point -- also puts the
                        # ground's horizon safely above the top of frame
CAM_FILL = 0.60         # fraction of frame height the subject occupies

PROJECT = "demo"
# Min Pods, not just Max Pods. Autoscale only adds a pod when it predicts more
# than AUTOSCALE_THRESHOLD_MINUTES (30) of work left, and eight frames finish
# in well under that -- so with Min Pods 1 the scheduler correctly decides one
# pod is enough and the sequence never demonstrates being spread over the farm.
# Asking for two up front is what makes the demo show what it claims to show;
# it costs about the same, because both pods are busy for half as long.
MIN_PODS = 2
MAX_PODS = 2
TASKS_PER_POD = 1
BUDGET = 1.0

# Degrees of yaw across the whole range, centred on 0 -- "rotating slightly".
YAW_SPAN = 28.0

RENDER_SUBDIR = "render"
PICTURE = "$HIP/{}/rpfarm_demo.$F4.exr".format(RENDER_SUBDIR)

TOPNET = "/obj/topnet1"
# The downstream-most node of the chain, and so the one Submit cooks.
COOK_TARGET = TOPNET + "/render"
SCHEDULER = TOPNET + "/rpfarm"

# Filled into the cost sticky note; kept here so one edit updates the scene.
EXPECTED_COST = "$0.10-0.35"
EXPECTED_MINUTES = "6-10"


# ---------------------------------------------------------------------------
# scene
# ---------------------------------------------------------------------------


def _set_static(node, parm_name, value):
    """Set a parm to a literal, dropping any expression first.

    Houdini stores an expression as a keyframe and ``Parm.set()`` on a keyframed
    parm is a silent no-op. Every frame range ships as ``$FSTART``/``$FEND``, so
    every one of them needs this -- without it the ROP Fetch quietly keeps the
    scene range and renders 240 frames instead of 8.
    """
    parm = node.parm(parm_name)
    parm.deleteAllKeyframes()
    parm.set(value)
    return parm


def build_materials():
    """Only the floor needs a material.

    ``testgeometry_pighead`` carries its own per-primitive
    ``shop_materialpath`` pointing at shaders inside the asset (skin, eyes),
    and a per-primitive assignment wins over the object-level one -- so a
    material made for the head here would be dead weight the artist has to
    reason about. Those shaders reference their maps as ``opdef:``, embedded
    in the factory HDA, so nothing extra ships to the farm either.
    """
    mat = hou.node("/mat")
    if mat is None:
        mat = hou.node("/").createNode("matnet", "mat")

    ground = mat.createNode("principledshader::2.0", "ground_mat")
    ground.parmTuple("basecolor").set((0.045, 0.048, 0.058))
    ground.parm("rough").set(0.30)
    ground.parm("reflect").set(0.5)

    mat.layoutChildren()
    return ground


def build_scene(ground_mat):
    """Pig head on a studio floor, three area lights, a 1280x720 camera."""
    obj = hou.node("/obj")

    geo = obj.createNode("geo", "pighead")
    head = geo.createNode("testgeometry_pighead", "pighead1")
    normal = geo.createNode("normal", "normal1")
    normal.setInput(0, head)
    normal.setDisplayFlag(True)
    normal.setRenderFlag(True)
    geo.layoutChildren()

    bbox = normal.geometry().boundingBox()
    centre = bbox.center()
    floor_y = bbox.minvec()[1]
    reach = max(bbox.sizevec()[0], bbox.sizevec()[1], bbox.sizevec()[2])

    # Yaw is an expression, not keyframes: one line the artist can read, and
    # each pod evaluates it for the single frame it was handed.
    geo.parm("ry").setExpression(
        "-{half} + ($F - {f0}) * {step}".format(
            half=YAW_SPAN / 2.0,
            f0=FRAME_START,
            step=YAW_SPAN / max(FRAME_END - FRAME_START, 1),
        )
    )
    # Rotate about the head's own centre, not the world origin.
    geo.parmTuple("p").set((centre[0], centre[1], centre[2]))

    ground = obj.createNode("geo", "ground")
    grid = ground.createNode("grid", "grid1")
    # Far larger than it needs to be on purpose: at 120x the subject the
    # grid's own far edge still drew a hard line across the top of frame.
    # The area lights are normalized, so the distant floor falls to black
    # by itself and the edge never arrives.
    grid.parm("sizex").set(reach * 800)
    grid.parm("sizey").set(reach * 800)
    grid.parm("rows").set(2)
    grid.parm("cols").set(2)
    grid.setDisplayFlag(True)
    grid.setRenderFlag(True)
    ground.parm("ty").set(floor_y)
    ground.parm("shop_materialpath").set(ground_mat.path())
    ground.layoutChildren()

    # Everything aims at this null, so the framing survives a nudge to any one
    # light or to the camera.
    aim = obj.createNode("null", "AIM_TARGET")
    aim.parmTuple("t").set((centre[0], centre[1] + bbox.sizevec()[1] * 0.15, centre[2]))

    cam = obj.createNode("cam", "cam1")
    cam.parm("resx").set(RESX)
    cam.parm("resy").set(RESY)
    cam.parm("focal").set(CAM_FOCAL)
    # Placed by angle and distance rather than by hand-tuned offsets, so the
    # framing is a consequence of the head's measured size: the subject fills
    # CAM_FILL of the frame height whatever the test geometry's scale is.
    height = bbox.sizevec()[1]
    v_aperture = cam.parm("aperture").eval() * float(RESY) / float(RESX)
    half_v_fov = math.atan(0.5 * v_aperture / CAM_FOCAL)
    distance = (height / CAM_FILL) / (2.0 * math.tan(half_v_fov))
    az, el = math.radians(CAM_AZIMUTH), math.radians(CAM_ELEVATION)
    aim_pos = aim.parmTuple("t").eval()
    cam.parmTuple("t").set((
        aim_pos[0] + distance * math.sin(az) * math.cos(el),
        aim_pos[1] + distance * math.sin(el),
        aim_pos[2] + distance * math.cos(az) * math.cos(el),
    ))
    cam.parm("lookatpath").set(aim.path())

    def area_light(name, pos, size, colour, intensity, exposure=0.0):
        light = obj.createNode("hlight::2.0", name)
        light.parm("light_type").set("grid")
        light.parmTuple("areasize").set(size)
        light.parmTuple("light_color").set(colour)
        light.parm("light_intensity").set(intensity)
        light.parm("light_exposure").set(exposure)
        light.parmTuple("t").set(pos)
        light.parm("lookatpath").set(aim.path())
        return light

    r = reach
    # Key: high and camera-right, warm, the light that draws the form.
    area_light(
        "key_light",
        (centre[0] + r * 2.4, centre[1] + r * 2.6, centre[2] + r * 2.0),
        (r * 2.2, r * 2.2),
        (1.0, 0.93, 0.84),
        72.0,
    )
    # Fill: big, soft, camera-left, cool, and weak -- it opens the shadow side
    # without flattening it.
    area_light(
        "fill_light",
        (centre[0] - r * 3.0, centre[1] + r * 1.1, centre[2] + r * 2.2),
        (r * 4.0, r * 4.0),
        (0.80, 0.87, 1.0),
        26.0,
    )
    # Rim: behind and above, opposite the key, to cut the head off the dark
    # background.
    area_light(
        "rim_light",
        (centre[0] - r * 1.6, centre[1] + r * 2.4, centre[2] - r * 2.6),
        (r * 1.6, r * 1.6),
        (0.88, 0.93, 1.0),
        140.0,
    )

    # A very dim uniform dome: no HDR to ship, but the background stops being
    # dead black and the shadows keep a little air in them.
    env = obj.createNode("envlight", "ambient")
    env.parmTuple("light_color").set((0.028, 0.033, 0.048))
    env.parm("light_intensity").set(1.0)

    obj.layoutChildren()
    return cam


def build_rop(cam):
    karma = hou.node("/out").createNode("karma", "karma_demo")
    karma.parm("camera").set(cam.path())
    karma.parm("engine").set(ENGINE)
    karma.parm("picture").set(PICTURE)
    karma.parm("trange").set(1)  # Render Frame Range
    _set_static(karma, "f1", FRAME_START)
    _set_static(karma, "f2", FRAME_END)
    _set_static(karma, "f3", 1)
    karma.parm("samplesperpixel").set(SAMPLES)
    karma.parm("denoiser").set("off")
    karma.parm("alfprogress").set(1)
    # The Karma ROP's intermediate USD defaults into $HOUDINI_TEMP_DIR, a
    # directory hou.fileReferences() reports and the upload then walks -- on a
    # machine that has rendered before it is full of other people's leftovers.
    # Inside $HIP it is deterministic, and on a fresh folder it simply does not
    # exist yet and is dropped.
    # Pod-local scratch for Karma's intermediate USD, and NOT $HIP.
    #
    # $HIP is the shared network volume. $RENDERID embeds the renderer's pid,
    # which is unique within a container and emphatically not across them, so
    # two pods rendering at the same time land on the same directory and one
    # overwrites the other's intermediate USD mid-render. The task still exits
    # 0 and still reports its output path, but no image is written -- verified
    # live: a one-pod cook lost nothing, the two-pod cook lost 2 frames of 8
    # with all eight ledger rows reading exit_code 0.
    #
    # $HOUDINI_TEMP_DIR is inside the container, so pods cannot collide, and
    # `rpfarm_usd` is a name nothing else writes, so on the artist's own
    # machine the directory does not exist and the upload's dependency walk
    # simply drops it -- which is the property the smoke fixture wanted from
    # $HIP in the first place.
    karma.parm("savetodirectory").set("$HOUDINI_TEMP_DIR/rpfarm_usd/$RENDERID")
    return karma


# ---------------------------------------------------------------------------
# TOP graph
# ---------------------------------------------------------------------------


def build_topnet(karma):
    topnet = hou.node("/obj").createNode("topnet", "topnet1")

    sched = topnet.createNode("runpodfarmscheduler", "rpfarm")
    sched.parm("rpfarm_project").set(PROJECT)
    # Empty -> gpu_priority from the artist's own ~/.rpfarm/config.toml, so the
    # scene carries no machine-specific GPU choice.
    sched.parm("rpfarm_gpulist").set("")
    sched.parm("rpfarm_minpods").set(MIN_PODS)
    sched.parm("rpfarm_maxpods").set(MAX_PODS)
    sched.parm("rpfarm_slots").set(TASKS_PER_POD)
    sched.parm("rpfarm_idletimeout").set(120)
    sched.parm("rpfarm_maxcost").set(BUDGET)
    sched.parm("rpfarm_downloadoutputs").set(1)
    sched.parm("rpfarm_verbose").set(1)
    topnet.parm("topscheduler").set(sched.path())

    upload = topnet.createNode("runpodfarmupload", "upload")
    upload.parm("rpfarm_mode").set("deps")
    upload.parm("rpfarm_project").set(PROJECT)
    # "auto" would compress and need zstd on the artist's machine; this payload
    # is a couple of MB, so there is nothing to gain and one dependency to lose.
    upload.parm("rpfarm_compress").set("off")

    gate = topnet.createNode("waitforall", "gate")
    gate.setInput(0, upload)

    render = topnet.createNode("ropfetch", "render")
    render.parm("roppath").set(karma.path())
    # "Frame Range", not "ROP Node Configuration": the latter hands the whole
    # range to the ROP as ONE work item that renders all eight frames in one
    # hython, on one pod. One item per frame is the point -- it is what spreads
    # the sequence over pods and gives one ledger row per frame.
    render.parm("framegeneration").set(1)
    _set_static(render, "range1", FRAME_START)
    _set_static(render, "range2", FRAME_END)
    _set_static(render, "range3", 1)
    render.setInput(0, gate)

    # No runpodfarm_download node here, deliberately.
    #
    # The scheduler's "Download Outputs" already fetches each frame the moment
    # its item succeeds, so frames land while the farm is still rendering --
    # which is the behaviour worth showing. A download node in Outputs mode
    # below the render would fetch the same files a SECOND time at the end of
    # the cook: the artist watched files arrive during the cook and then saw
    # eight more download items re-transfer them at the end. That node is for
    # pulling folders nothing reported as an output; duplicating the scheduler
    # is not its job. The node itself now warns when both are on.
    render.setDisplayFlag(True)

    stats = topnet.createNode("runpodfarmstats", "stats")
    stats.parm("rpfarm_project").set(PROJECT)
    stats.parm("rpfarm_usebilling").set(0)

    submit = topnet.createNode("null", "SUBMIT_TO_FARM")
    _build_submit_interface(submit)

    upload.matchCurrentDefinition()

    topnet.layoutChildren([upload, gate, render])
    _place_side_nodes(topnet, submit, stats)
    _add_sticky_notes(topnet, submit, stats)
    return topnet


def _build_submit_interface(node):
    """Two big buttons and a status field, on a node impossible to miss."""
    node.setColor(hou.Color((0.20, 0.75, 0.35)))
    node.setUserData("nodeshape", "burst")
    node.setComment("Одна кнопка — и всё")
    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)

    ptg = node.parmTemplateGroup()

    submit = hou.ButtonParmTemplate(
        "rpdemo_submit",
        "▶     О Т П Р А В И Т Ь   Н А   Ф Е Р М У     ◀",
        script_callback="hou.session.rpfarm_demo_submit(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )
    submit.setHelp(
        "Сохраняет сцену и отправляет все {} кадров на ферму одним куком "
        "(нода render, один вызов cookWorkItems). Больше ничего нажимать "
        "не нужно.".format(FRAME_END - FRAME_START + 1)
    )

    kill = hou.ButtonParmTemplate(
        "rpdemo_kill",
        "■     О С Т А Н О В И Т Ь   И   П О Г А С И Т Ь   П О Д Ы",
        script_callback="hou.session.rpfarm_demo_kill(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )
    kill.setHelp(
        "Отменяет текущий кук и терминирует все ваши поды на RunPod, включая "
        "sync-под. То же, что `rpfarm farm kill --all` + `--sync`."
    )

    folder = hou.ButtonParmTemplate(
        "rpdemo_open",
        "Открыть папку с кадрами",
        script_callback="hou.session.rpfarm_demo_open_folder(kwargs)",
        script_callback_language=hou.scriptLanguage.Python,
    )

    status = hou.StringParmTemplate(
        "rpdemo_status", "Что происходит", 1, default_value=("",)
    )
    status.setTags({"editor": "1", "editorlines": "6-14"})

    sep = hou.SeparatorParmTemplate("rpdemo_sep")

    for pt in (submit, kill, sep, folder, status):
        ptg.append(pt)
    node.setParmTemplateGroup(ptg)


def _place_side_nodes(topnet, submit, stats):
    """Submit above the chain's head, stats off to the right of its tail."""
    chain = [topnet.node(n) for n in ("upload", "gate", "render")]
    left = min(n.position()[0] for n in chain)
    top = max(n.position()[1] for n in chain)
    bottom = min(n.position()[1] for n in chain)

    submit.setPosition(hou.Vector2(left - 3.6, top + 1.0))
    stats.setPosition(hou.Vector2(left + 4.2, bottom))
    topnet.node("rpfarm").setPosition(hou.Vector2(left - 3.6, top - 2.2))


def _sticky(parent, name, text, pos, size, colour, text_size=0.36):
    note = parent.createStickyNote(name)
    note.setText(text)
    note.setPosition(hou.Vector2(*pos))
    note.setSize(hou.Vector2(*size))
    note.setColor(hou.Color(colour))
    note.setTextSize(text_size)
    note.setDrawBackground(True)
    return note


def _add_sticky_notes(topnet, submit, stats):
    sx, sy = submit.position()

    _sticky(
        topnet,
        "note_submit",
        "ЖМИ СЮДА, БОЛЬШЕ НИЧЕГО НЕ НУЖНО\n"
        "\n"
        "Нода SUBMIT_TO_FARM слева — на ней кнопка\n"
        "«ОТПРАВИТЬ НА ФЕРМУ».\n"
        "\n"
        "Она сама сохранит сцену и отправит все 8 кадров\n"
        "на ферму одним куком. Не нажимай Cook на других\n"
        "нодах: каждый лишний кук — это лишний платный\n"
        "GPU-под.\n"
        "\n"
        "Если нужно всё остановить — вторая кнопка,\n"
        "«ОСТАНОВИТЬ И ПОГАСИТЬ ПОДЫ».",
        (sx - 0.6, sy + 5.4),
        (7.4, 4.6),
        (0.16, 0.42, 0.20),
    )

    chain = [topnet.node(n) for n in ("upload", "gate", "render")]
    cx = min(n.position()[0] for n in chain)
    cy = max(n.position()[1] for n in chain)

    _sticky(
        topnet,
        "note_chain",
        "ЧТО ДЕЛАЕТ КАЖДАЯ НОДА\n"
        "\n"
        "upload    — заливает сцену и всё, что она тянет, на ферму\n"
        "gate      — ждёт, пока зальётся всё; без него каждый кадр\n"
        "            посчитался бы дважды и стоил бы вдвое\n"
        "render    — 8 кадров Karma CPU, один work item на кадр,\n"
        "            кадры разъезжаются по разным подам\n"
        "\n"
        "Готовые EXR приезжают сами, по мере готовности каждого\n"
        "кадра — это делает планировщик (Download Outputs).\n"
        "Отдельной ноды download здесь нет намеренно: с включённой\n"
        "жадной докачкой она качала бы всё то же самое второй раз.\n"
        "\n"
        "stats     — справа, к графу не подключена: покажет, сколько\n"
        "            это стоило. Жми на ней Refresh после кука.",
        (cx + 3.2, cy + 1.9),
        (8.8, 4.0),
        (0.20, 0.22, 0.30),
    )

    _sticky(
        topnet,
        "note_output",
        "КУДА ПРИДУТ КАДРЫ\n"
        "\n"
        "~/Desktop/rpfarm_demo/render/\n"
        "     rpfarm_demo.0001.exr … rpfarm_demo.0008.exr\n"
        "\n"
        "Они появляются по одному, прямо во время кука, —\n"
        "не жди все восемь сразу.\n"
        "\n"
        "Рядом лежит preview_expected.png — так должен\n"
        "выглядеть кадр. Если получилось похоже, ферма\n"
        "работает.",
        (cx + 3.2, cy - 3.0),
        (8.8, 4.0),
        (0.20, 0.22, 0.30),
    )

    _sticky(
        topnet,
        "note_cost",
        "ДЕНЬГИ\n"
        "\n"
        "Потолок — {budget} на один кук (параметр Budget\n"
        "на ноде rpfarm). На 80% ферма предупредит,\n"
        "на 100% перестанет поднимать поды,\n"
        "на 120% погасит всё сама.\n"
        "\n"
        "Этот прогон: примерно {cost}, {minutes} минут.\n"
        "{pods} GPU-пода, по одному кадру на под за раз:\n"
        "восемь кадров разъедутся между ними.\n"
        "\n"
        "Поды гаснут сами в конце кука. Кроме дешёвого\n"
        "sync-пода (~$0.06/ч) — его снимает кнопка\n"
        "«ОСТАНОВИТЬ И ПОГАСИТЬ ПОДЫ».".format(
            budget="${:.2f}".format(BUDGET), cost=EXPECTED_COST,
            minutes=EXPECTED_MINUTES, pods=MAX_PODS
        ),
        (sx - 0.6, sy - 8.2),
        (7.4, 4.8),
        (0.34, 0.26, 0.14),
    )


# ---------------------------------------------------------------------------
# the button code, embedded in the scene
# ---------------------------------------------------------------------------


SESSION_SOURCE = '''"""Buttons for the rpfarm demo scene (SUBMIT_TO_FARM).

Lives in the scene's Python source editor so the .hip is self-contained.
Everything here is defensive on purpose: a demo that throws a traceback at
the person deciding whether to trust the farm has already failed.
"""

import os
import pathlib
import sys

import hou

TOPNET = "{topnet}"
COOK_TARGET = "{cook_target}"
SCHEDULER = "{scheduler}"
RENDER_SUBDIR = "{render_subdir}"


def _status(node, text):
    parm = node.parm("rpdemo_status")
    if parm is not None:
        parm.set(text)
    print("[rpfarm-demo] " + text.replace("\\n", " | "))


def _rpfarm():
    """Import the rpfarm package the same way the HDAs do."""
    root = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from rpfarm import config as rpcfg
    from rpfarm import pods as rppods
    from rpfarm.runpod_api import RunPodAPI
    return rpcfg, rppods, RunPodAPI


def _context():
    return hou.node(TOPNET).getPDGGraphContext()


def rpfarm_demo_submit(kwargs):
    """Save the scene and cook the downstream-most node once, non-blocking.

    One cookWorkItems call on the downstream-most node and nothing else:
    cooking two nodes in two calls starts two independent PDG cooks and pays
    for a second GPU pod. Frames come home by themselves as each item
    succeeds -- the scheduler's "Download Outputs" does that during the cook.
    """
    node = kwargs["node"]
    try:
        if _context().cooking:
            _status(node, "Кук уже идёт. Второй запуск поднял бы ещё один "
                          "платный под, поэтому ничего не делаю.\\n"
                          "Чтобы остановить — кнопка ниже.")
            return
    except Exception:
        pass

    try:
        hou.hipFile.save()
    except hou.OperationFailed as e:
        _status(node, "Не смог сохранить сцену: {{}}".format(e))
        return

    target = hou.node(COOK_TARGET)
    if target is None:
        _status(node, "Не нашёл ноду {{}} — граф изменён?".format(COOK_TARGET))
        return

    _status(node, "Отправляю на ферму…\\n"
                  "Сцена сохранена. Первые пара минут — заливка и запуск подов,\\n"
                  "кадры пойдут после. Прогресс видно прямо на нодах графа.")
    try:
        target.cookWorkItems(block=False, save_prompt=False)
    except hou.Error as e:
        _status(node, "Кук не стартовал: {{}}".format(e))
        return
    _watch(node)


def _watch(node):
    """Narrate the cook in the status field. GUI only, and never fatal."""
    if not hasattr(hou, "ui"):
        return
    state = {{"n": 0}}

    def poll():
        try:
            ctx = _context()
            done = _frames_on_disk()
            if ctx.cooking:
                state["n"] += 1
                _status(node, "Идёт кук…  готовых кадров: {{}}/{{}}\\n"
                              "Поды и стоимость — на ноде rpfarm, вкладка Status.".format(
                                  done, {frames}))
            else:
                hou.ui.removeEventLoopCallback(poll)
                _status(node, "Кук закончился. Кадров на диске: {{}}/{{}}\\n"
                              "Папка: {{}}\\n"
                              "Сколько стоило — нода stats, кнопка Refresh.".format(
                                  done, {frames}, _render_dir()))
        except Exception:
            try:
                hou.ui.removeEventLoopCallback(poll)
            except Exception:
                pass

    try:
        hou.ui.addEventLoopCallback(poll)
    except Exception:
        pass


def _render_dir():
    return os.path.join(hou.text.expandString("$HIP"), RENDER_SUBDIR)


def _frames_on_disk():
    d = _render_dir()
    try:
        return len([f for f in os.listdir(d) if f.endswith(".exr")])
    except OSError:
        return 0


def rpfarm_demo_kill(kwargs):
    """Cancel the cook, then terminate every pod of this user, sync included."""
    node = kwargs["node"]
    lines = []
    try:
        ctx = _context()
        if ctx.cooking:
            ctx.cancelCook()
            lines.append("Кук отменён.")
    except Exception as e:
        lines.append("Отмена кука не прошла: {{}}".format(e))

    try:
        rpcfg, rppods, RunPodAPI = _rpfarm()
        cfg = rpcfg.load()
        api = RunPodAPI(cfg.api_key)
        sync_name = rppods.sync_pod_name(cfg.user)
        victims = list(rppods.find_orphans(api, cfg.user))
        victims += [p for p in api.list_pods(sync_name) if p.get("name") == sync_name]
        for pod in victims:
            api.terminate_pod(pod["id"])
        names = ", ".join(p.get("name", p["id"]) for p in victims) or "-"
        lines.append("Погашено подов: {{}} ({{}})".format(len(victims), names))
    except Exception as e:
        lines.append("Не удалось погасить поды: {{}}\\n"
                     "Из терминала: rpfarm farm kill --all && rpfarm farm kill --sync".format(e))
    _status(node, "\\n".join(lines))


def rpfarm_demo_open_folder(kwargs):
    node = kwargs["node"]
    d = _render_dir()
    os.makedirs(d, exist_ok=True)
    try:
        import subprocess
        subprocess.Popen(["open", d])
    except Exception as e:
        _status(node, "Открой вручную: {{}}  ({{}})".format(d, e))
'''


def install_session_module():
    source = SESSION_SOURCE.format(
        topnet=TOPNET,
        cook_target=COOK_TARGET,
        scheduler=SCHEDULER,
        render_subdir=RENDER_SUBDIR,
        frames=FRAME_END - FRAME_START + 1,
    )
    hou.setSessionModuleSource(source)
    return source


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _obj_sticky(dest_dir):
    obj = hou.node("/obj")
    topnet = obj.node("topnet1")
    tx, ty = topnet.position()
    note = obj.createStickyNote("note_start_here")
    note.setText(
        "НАЧАЛО ЗДЕСЬ\n"
        "\n"
        "Зайди внутрь ноды topnet1 (двойной клик).\n"
        "Там одна зелёная нода SUBMIT_TO_FARM и на ней\n"
        "кнопка «ОТПРАВИТЬ НА ФЕРМУ». Это всё, что нужно.\n"
        "\n"
        "Готовые кадры лягут в\n"
        "{}/{}/".format(dest_dir, RENDER_SUBDIR)
    )
    note.setPosition(hou.Vector2(tx - 1.0, ty + 2.6))
    note.setSize(hou.Vector2(7.6, 3.2))
    note.setColor(hou.Color((0.16, 0.42, 0.20)))
    note.setTextSize(0.38)
    note.setDrawBackground(True)
    topnet.setColor(hou.Color((0.45, 0.30, 0.75)))


def main(argv):
    dest_dir = os.path.abspath(os.path.expanduser(argv[0] if argv else DEFAULT_DEST_DIR))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, HIP_NAME)

    hou.hipFile.clear(suppress_save_prompt=True)
    hou.setFrame(FRAME_START)
    hou.playbar.setFrameRange(FRAME_START, FRAME_END)
    hou.playbar.setPlaybackRange(FRAME_START, FRAME_END)

    ground_mat = build_materials()
    cam = build_scene(ground_mat)
    karma = build_rop(cam)
    build_topnet(karma)
    install_session_module()
    _obj_sticky(dest_dir)

    # A .hip carries its own variable table and its $JOB wins over the
    # environment when the file is loaded. Everything under $JOB is what the
    # upload ships, so it has to be this folder.
    hou.putenv("JOB", dest_dir)

    hou.hipFile.save(dest)
    print("wrote {} ({} bytes)".format(dest, os.path.getsize(dest)))
    return _verify(dest, dest_dir)


def _verify(dest, dest_dir):
    """Reload and assert the things that are expensive to get wrong."""
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.load(dest, suppress_save_prompt=True)

    ok = True

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print("FAIL: " + msg)
            ok = False

    karma = hou.node("/out/karma_demo")
    for name, want in (("f1", FRAME_START), ("f2", FRAME_END), ("f3", 1), ("trange", 1)):
        check(abs(float(karma.parm(name).eval()) - want) < 1e-6,
              "karma_demo/{} = {!r}, expected {!r}".format(name, karma.parm(name).eval(), want))
    check(karma.parm("engine").eval() == ENGINE,
          "karma_demo/engine = {!r}, expected {!r}".format(
              karma.parm("engine").eval(), ENGINE))
    # XPU is a legitimate choice; anything else is a typo that costs a cook.
    check(ENGINE in ("cpu", "xpu"), "ENGINE = {!r} is not a Karma engine".format(ENGINE))
    check(karma.parm("picture").rawValue() == PICTURE,
          "karma_demo/picture = {!r}".format(karma.parm("picture").rawValue()))
    # Never $HIP: that is the shared volume, and two pods collide there.
    check(karma.parm("savetodirectory").rawValue().startswith("$HOUDINI_TEMP_DIR"),
          "karma_demo/savetodirectory = {!r} -- must be pod-local scratch".format(
              karma.parm("savetodirectory").rawValue()))

    render = hou.node("/obj/topnet1/render")
    for name, want in (("range1", FRAME_START), ("range2", FRAME_END),
                       ("range3", 1), ("framegeneration", 1)):
        check(abs(float(render.parm(name).eval()) - want) < 1e-6,
              "render/{} = {!r}, expected {!r}".format(name, render.parm(name).eval(), want))
    check(render.parm("roppath").eval() == "/out/karma_demo",
          "render/roppath = {!r}".format(render.parm("roppath").eval()))

    sched = hou.node(SCHEDULER)
    for name, want in (("rpfarm_minpods", MIN_PODS), ("rpfarm_maxpods", MAX_PODS),
                       ("rpfarm_slots", TASKS_PER_POD),
                       ("rpfarm_maxcost", BUDGET), ("rpfarm_downloadoutputs", 1)):
        check(abs(float(sched.parm(name).eval()) - want) < 1e-6,
              "rpfarm/{} = {!r}, expected {!r}".format(name, sched.parm(name).eval(), want))
    check(hou.node(TOPNET).parm("topscheduler").eval() == SCHEDULER,
          "topnet topscheduler = {!r}".format(hou.node(TOPNET).parm("topscheduler").eval()))

    target = hou.node(COOK_TARGET)
    check(target.isDisplayFlagSet(), "the cooked node does not carry the display flag")
    check(target.inputs() and target.inputs()[0].name() == "gate",
          "render is not wired to the waitforall gate")
    # Both download paths on at once fetches every frame twice -- the artist
    # saw it happen. The scheduler's greedy download is the one kept.
    check(hou.node("/obj/topnet1/download") is None,
          "a runpodfarm_download node is back in the chain while the scheduler "
          "is also downloading outputs: every frame would be fetched twice")

    # An unlocked HDA subnet that has drifted from its definition would freeze
    # a stale copy of its generate script into this .hip and ignore every
    # future HDA rebuild. Three live runs in Task 15 downloaded nothing for
    # exactly this reason.
    node = hou.node("/obj/topnet1/upload")
    check(node.matchesCurrentDefinition(),
          "upload has an edited/frozen internal network")

    job = hou.text.expandString("$JOB")
    check(os.path.realpath(job) == os.path.realpath(dest_dir),
          "$JOB = {!r}, expected {!r}".format(job, dest_dir))
    hip = hou.text.expandString("$HIP")
    check(os.path.realpath(hip) == os.path.realpath(dest_dir),
          "$HIP = {!r}, expected {!r}".format(hip, dest_dir))

    submit = hou.node("/obj/topnet1/SUBMIT_TO_FARM")
    check(submit is not None and submit.parm("rpdemo_submit") is not None,
          "SUBMIT_TO_FARM is missing its Submit button")
    # The session module is compiled on load; if it were broken the artist
    # would meet a traceback before he met the button.
    check(callable(getattr(hou.session, "rpfarm_demo_submit", None)),
          "hou.session.rpfarm_demo_submit is not callable after reload")
    check(callable(getattr(hou.session, "rpfarm_demo_kill", None)),
          "hou.session.rpfarm_demo_kill is not callable after reload")

    print("verify: {}".format("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
