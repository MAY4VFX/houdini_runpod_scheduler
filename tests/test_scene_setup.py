"""The TAB-menu setup tool, driven without Houdini.

``rpfarm.scene_setup.build`` takes a duck-typed parent on purpose, so the
whole builder -- what it creates, what it adopts, how it wires and what it
preconfigures -- is testable here. The parts that genuinely need ``hou``
(finding the network the TAB was pressed in, listing ROPs) are thin and are
verified in a live session instead.
"""

from __future__ import annotations

import pytest

from rpfarm import scene_setup as ss


# -- a network that behaves like the bit of hou.Node this uses ---------------


class FakeParm:
    def __init__(self, value="", default=None):
        self.value = value
        #: What Houdini would have put here. A fresh topnet's topscheduler is
        #: NOT empty -- it points at the localscheduler the topnet made for
        #: itself -- so "at its default" is the only way to tell Houdini's
        #: value from the artist's.
        self.default = value if default is None else default
        self.sets = 0

    def set(self, value):
        self.value = value
        self.sets += 1

    def evalAsString(self):
        return str(self.value)

    def isAtDefault(self):
        return self.value == self.default


class FakeType:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class FakeNode:
    def __init__(self, type_name, name, parent, parms=()):
        self._type = FakeType(type_name)
        self._name = name
        self._parent = parent
        self.parms = {p: FakeParm() for p in parms}
        self._inputs = [None, None]
        self.display = False
        self.laid_out = False

    def type(self):
        return self._type

    def path(self):
        return "{}/{}".format(self._parent.path() if self._parent else "", self._name)

    def parm(self, name):
        return self.parms.get(name)

    def inputs(self):
        return tuple(self._inputs)

    def setInput(self, index, source):
        self._inputs[index] = source

    def setDisplayFlag(self, on):
        self.display = on


#: The parms each created type actually has, so a typo in a parm name in
#: scene_setup shows up here as a silently unset parm rather than passing.
PARMS = {
    ss.SCHEDULER_TYPE: ("rpfarm_project", "rpfarm_maxpods", "rpfarm_minpods"),
    ss.UPLOAD_TYPE: ("rpfarm_project", "rpfarm_mode", "rpfarm_scope", "rpfarm_compress"),
    ss.GATE_TYPE: (),
    ss.FETCH_TYPE: ("roppath", "framegeneration"),
}


class FakeNetwork:
    def __init__(self, path="/obj/topnet1", topscheduler="localscheduler",
                 with_localscheduler=True):
        self._path = path
        self._children = []
        self.parms = {"topscheduler": FakeParm(topscheduler)}
        self.laid_out = False
        # A real topnet arrives with a localscheduler of its own, already
        # named in topscheduler. Modelling that is the difference between
        # this suite and the live session that caught the bug.
        if with_localscheduler:
            self.createNode(ss.LOCAL_SCHEDULER_TYPE, "localscheduler")

    def path(self):
        return self._path

    def children(self):
        return tuple(self._children)

    def parm(self, name):
        return self.parms.get(name)

    def layoutChildren(self):
        self.laid_out = True

    def node(self, path):
        """Resolve a child by relative name, the way hou.Node.node does."""
        name = path.rstrip("/").rsplit("/", 1)[-1]
        for child in self._children:
            if child._name == name:
                return child
        return None

    def createNode(self, type_name, node_name):
        node = FakeNode(type_name, node_name, self, PARMS.get(type_name, ()))
        self._children.append(node)
        return node


def roles(pairs):
    return {role for role, _ in pairs}


# -- what gets built ---------------------------------------------------------


def test_a_fresh_network_gets_the_whole_graph_wired():
    net = FakeNetwork()
    result = ss.build(net, project="airship", frames=48, rop_candidates=["/out/karma1"])

    assert roles(result.created) == {"scheduler", "upload", "gate", "render", "download"}
    assert not result.reused

    by_type = {n.type().name(): n for n in net.children()}
    gate, upload = by_type[ss.GATE_TYPE], by_type[ss.UPLOAD_TYPE]
    fetch = by_type[ss.FETCH_TYPE]
    assert gate.inputs()[0] is upload
    assert fetch.inputs()[0] is gate
    # Only the bottom node carries the display flag: cooking anything else,
    # or cooking twice, pays for a second GPU pod.
    assert by_type[ss.DOWNLOAD_TYPE].display is True
    assert by_type[ss.DOWNLOAD_TYPE].inputs()[0] is fetch
    assert net.laid_out is True


def test_the_scheduler_becomes_the_networks_scheduler():
    net = FakeNetwork()
    result = ss.build(net)
    sched = next(n for n in net.children() if n.type().name() == ss.SCHEDULER_TYPE)
    assert net.parm("topscheduler").value == sched.path()
    assert result.scheduler_assigned is True


def test_a_fresh_topnets_own_localscheduler_is_not_a_choice():
    """Houdini creates a topnet already pointing at its own localscheduler,
    and does it by SETTING the parm, so it is not "at default" either.
    Found live: treating that as "the artist picked one" left every fresh
    network without a farm scheduler, which is the whole point of the tool."""
    net = FakeNetwork()
    assert net.parm("topscheduler").value == "localscheduler"
    result = ss.build(net)
    sched = next(n for n in net.children() if n.type().name() == ss.SCHEDULER_TYPE)
    assert net.parm("topscheduler").value == sched.path()
    assert result.scheduler_assigned is True


def test_an_existing_scheduler_choice_is_not_taken_away():
    net = FakeNetwork(topscheduler="deadline", with_localscheduler=False)
    net.createNode("deadlinescheduler", "deadline")
    result = ss.build(net)
    assert net.parm("topscheduler").value == "deadline"
    assert result.scheduler_assigned is False
    assert any("already" in note for note in result.notes)


def test_a_scheduler_path_pointing_at_nothing_is_repaired():
    """Not a choice -- a leftover. Replacing it is a repair, not an override."""
    net = FakeNetwork(topscheduler="/obj/topnet1/deleted_long_ago",
                      with_localscheduler=False)
    result = ss.build(net)
    sched = next(n for n in net.children() if n.type().name() == ss.SCHEDULER_TYPE)
    assert net.parm("topscheduler").value == sched.path()
    assert result.scheduler_assigned is True


def test_download_is_the_explicit_delivery_stage():
    """The scheduler downloads outputs during the cook; a second node in
    Upstream Outputs mode walks the same files again. Why is documented in
    the README and this module -- not repeated to the artist at runtime."""
    net = FakeNetwork()
    ss.build(net)
    assert ss.DOWNLOAD_TYPE in {n.type().name() for n in net.children()}


def test_the_scene_is_what_configures_the_nodes():
    net = FakeNetwork()
    ss.build(net, project="airship", frames=2, rop_candidates=["/stage/usdrender_rop1"])
    by_type = {n.type().name(): n for n in net.children()}
    sched, upload = by_type[ss.SCHEDULER_TYPE], by_type[ss.UPLOAD_TYPE]
    fetch = by_type[ss.FETCH_TYPE]

    assert sched.parm("rpfarm_project").value == "airship"
    assert upload.parm("rpfarm_project").value == "airship"
    assert sched.parm("rpfarm_maxpods").value == 2      # two frames, two pods
    assert upload.parm("rpfarm_scope").value == "branch"
    assert upload.parm("rpfarm_compress").value == "auto"
    assert fetch.parm("roppath").value == "/stage/usdrender_rop1"
    # Frame Range: one work item per frame is what makes this a farm rather
    # than one pod rendering everything.
    assert fetch.parm("framegeneration").value == 1


def test_an_unsaved_scene_leaves_the_project_parm_alone():
    """An empty project means "the scene did not say" -- writing "" over the
    node's own default expression would replace a live default with a
    literal blank."""
    net = FakeNetwork()
    ss.build(net, project="")
    upload = next(n for n in net.children() if n.type().name() == ss.UPLOAD_TYPE)
    assert upload.parm("rpfarm_project").sets == 0


# -- running it twice --------------------------------------------------------


def test_a_second_run_adopts_instead_of_duplicating():
    net = FakeNetwork()
    ss.build(net, project="airship", frames=10, rop_candidates=["/out/karma1"])
    before = len(net.children())

    again = ss.build(net, project="airship", frames=10, rop_candidates=["/out/karma1"])

    assert len(net.children()) == before
    assert not again.created
    assert roles(again.reused) == {"scheduler", "upload", "gate", "render", "download"}
    assert any("already set up" in line for line in ss.report(again))


def test_a_renamed_scheduler_is_still_a_scheduler():
    """Idempotency keys on the node TYPE: a second scheduler in one network
    would be a second farm identity, whatever it is called."""
    net = FakeNetwork()
    net.createNode(ss.SCHEDULER_TYPE, "my_farm")
    result = ss.build(net)
    assert [n.type().name() for n in net.children()].count(ss.SCHEDULER_TYPE) == 1
    assert ("scheduler", "/obj/topnet1/my_farm") in result.reused


def test_wiring_the_artist_changed_is_reported_not_overwritten():
    net = FakeNetwork()
    ss.build(net)
    fetch = next(n for n in net.children() if n.type().name() == ss.FETCH_TYPE)
    other = net.createNode("genericgenerator", "probe")
    fetch.setInput(0, other)

    result = ss.build(net)
    assert fetch.inputs()[0] is other
    assert any("left alone" in note for note in result.notes)


# -- the derivations ---------------------------------------------------------


@pytest.mark.parametrize("hip_dir,hip_file,expected", [
    ("/Users/artist/BS/airship", "airship_v016.hip", "airship"),
    ("/Users/artist/BS/airship/scenes", "airship_v016.hip", "airship"),
    ("/Users/artist/BS/airship/hip/wip", "shot_010.hip", "airship"),
    ("C:\\\\work\\\\shows\\\\dragon\\\\scenes", "dragon_v3.hip", "dragon"),
    ("", "airship_v016.hip", "airship"),
    ("", "airship.v2.hip", "airship"),
    ("", "untitled.hip", ""),
    ("/", "untitled.hip", ""),
])
def test_project_name_comes_from_the_scene(hip_dir, hip_file, expected):
    assert ss.project_name(hip_dir, hip_file) == expected


def test_a_home_directory_is_not_a_project():
    """The artist's $JOB was his home directory, which is how the farm ended
    up laid out as a mirror of it. The scene file's own name is the better
    answer than "may"."""
    import os

    home = os.path.expanduser("~")
    assert ss.project_name(home, "airship_v016.hip") == "airship"


@pytest.mark.parametrize("frames,expected", [
    (1, 1), (2, 2), (4, 4), (48, 4), (0, 1), (-3, 1), (None, 1),
])
def test_never_more_machines_than_frames(frames, expected):
    assert ss.pod_ceiling(frames) == expected


@pytest.mark.parametrize("start,end,step,expected", [
    (1, 1, 1, 1), (1, 3, 1, 3), (1001, 1048, 1, 48), (1, 10, 2, 5), (1, 10, 0, 10),
    ("a", "b", 1, 1),
])
def test_frame_count(start, end, step, expected):
    assert ss.frame_count(start, end, step) == expected


@pytest.mark.parametrize("candidates,expected", [
    (["/out/karma1"], "/out/karma1"),
    ([], ""),
    (["/out/karma1", "/out/mantra1"], ""),
])
def test_one_rop_is_an_answer_two_is_a_question(candidates, expected):
    assert ss.choose_rop(candidates) == expected


def test_the_report_names_the_field_to_fill_when_it_cannot_choose():
    net = FakeNetwork()
    result = ss.build(net, rop_candidates=["/out/karma1", "/out/mantra1"])
    text = "\n".join(ss.report(result))
    assert "ROP Path is empty" in text and "2 renderable ROPs" in text


def test_the_report_says_nothing_the_nodes_already_show():
    """Ruling R54. Project, Max Pods and Compression are on the parameters;
    repeating them is the noise the owner asked us to stop making."""
    net = FakeNetwork()
    result = ss.build(net, project="airship", frames=48, rop_candidates=["/out/karma1"])
    text = "\n".join(ss.report(result))
    for noise in ("airship", "Max Pods", "Compression", "Project:", "Download"):
        assert noise not in text, noise
    assert len(ss.report(result)) <= 3


def test_the_tool_never_opens_a_dialog():
    """The whole point of the complaint: it built the graph, the graph is the
    report. Asserted on the source because there is no hou here to open one."""
    import inspect

    source = inspect.getsource(ss)
    assert "displayMessage" not in source
