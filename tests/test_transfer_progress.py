from rpfarm.progress import TransferProgress


class PDG:
    def __init__(self):
        self.values = {}
    def setStringAttrib(self, name, value, index):
        self.values[name] = value
    setIntAttrib = setStringAttrib
    setFloatAttrib = setStringAttrib
    def workItemSetCustomState(self, value, **kwargs):
        self.values['native_state'] = value
    def workItemSetCookPercent(self, value, **kwargs):
        self.values['native_percent'] = value


def test_two_transfer_parts_have_one_total_and_honest_eta():
    pdg = PDG()
    p = TransferProgress(pdg)
    p.plan(1000, 4, skipped=2)
    p.begin_transfer()
    p.details({'transfers': 2})
    p(200, 400, 100)
    assert pdg.values['bytes_done'] == 200
    assert pdg.values['bytes_total'] == 1000
    assert pdg.values['eta_seconds'] == 8
    assert pdg.values['native_percent'] == 20
    assert pdg.values['native_state'] == 'Transferring'
    p.end_transfer(400)
    p.begin_transfer()
    p.details({'transfers': 1})
    p(100, 600, 100)
    assert pdg.values['bytes_done'] == 500
    assert pdg.values['percent'] == 50
    assert pdg.values['files_skipped'] == 2
    assert pdg.values['files_done'] == 3


def test_all_files_skipped_and_unknown_speed_do_not_invent_eta():
    pdg = PDG()
    p = TransferProgress(pdg)
    p.plan(0, 0, skipped=12)
    p.finish()
    assert pdg.values['phase'] == 'Complete'
    assert pdg.values['bytes_done'] == 0
    assert pdg.values['percent'] == 100
    assert pdg.values['eta_seconds'] == 0
    assert pdg.values['native_percent'] == 100


def test_current_files_and_phase_are_visible_before_transfer():
    pdg = PDG()
    p = TransferProgress(pdg)
    p.phase('Checking farm')
    assert pdg.values['progress'] == 'Checking farm'
    p.details({'transferring': [{'name': 'a.exr'}, {'name': 'b.exr'}]})
    assert pdg.values['current_files'] == 'a.exr\nb.exr'


def test_network_progress_does_not_claim_job_complete_before_unpacking():
    pdg = PDG()
    p = TransferProgress(pdg)
    p.plan(100, 1)
    p(100, 100, 20)
    p.phase('Unpacking on farm')
    assert pdg.values['phase'] == 'Unpacking on farm'
    assert pdg.values['progress'] == 'Unpacking on farm'
    assert pdg.values['native_percent'] < 100


def test_attribute_types_never_change_between_updates():
    """PDG refuses to change an attribute's type and the error is swallowed:
    eta_seconds was created Int (-1) and then sent as Float, so it stayed -1
    for a whole live transfer (2026-11-01)."""
    from rpfarm.progress import TransferProgress

    class Rec:
        def __init__(self):
            self.kinds = {}

        def _rec(self, kind, name):
            assert self.kinds.setdefault(name, kind) == kind, (name, self.kinds[name], kind)

        def setIntAttrib(self, name, value, i):
            self._rec("Int", name)

        def setFloatAttrib(self, name, value, i):
            self._rec("Float", name)

        def setStringAttrib(self, name, value, i):
            self._rec("String", name)

    rec = Rec()
    p = TransferProgress(rec)
    p.plan(1000, 3)
    p.begin_transfer()
    p(100, 1000, 50.0)
    p(500, 1000, 0.0)
    p.finish()
