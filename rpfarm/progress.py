"""Transfer state reported as ordinary PDG work-item attributes."""


class TransferProgress:
    def __init__(self, pdgcmd=None):
        self.pdg = pdgcmd
        self.total = None
        self.offset = 0
        self.done = 0
        self.files_done = self.file_offset = 0
        self.name = ''

    def _set(self, name, value):
        if self.pdg is None:
            return
        kind = 'String' if isinstance(value, str) else 'Int' if isinstance(value, int) else 'Float'
        try:
            getattr(self.pdg, 'set' + kind + 'Attrib')(name, value, 0)
        except Exception:
            pass  # Work-item exit status remains authoritative if RPC is unavailable.

    def phase(self, name):
        self.name = name
        self._native('workItemSetCustomState', name)
        self._set('phase', name)
        self._set('progress', name)
        self._set('eta_seconds', -1)

    def _native(self, method, value):
        function = getattr(self.pdg, method, None)
        if function:
            try:
                function(value, to_stdout=False)
            except Exception:
                pass

    def plan(self, total_bytes, files, skipped=0):
        self.total = max(0, total_bytes)
        self.offset = self.done = 0
        self.files_done = self.file_offset = 0
        self._set('bytes_total', self.total)
        self._set('bytes_done', 0)
        self._set('files_to_transfer', files)
        self._set('files_skipped', skipped)

    def begin_transfer(self):
        self.file_offset = self.files_done
        self.phase('Transferring')

    def end_transfer(self, actual_bytes):
        self.offset += actual_bytes

    def details(self, stats):
        self._set('current_files', '\n'.join(
            str(row.get('name', '')) for row in stats.get('transferring', []) if row.get('name')))
        self.files_done = self.file_offset + int(stats.get('transfers', 0))
        self._set('files_done', self.files_done)

    def __call__(self, done, total, speed):
        from .preflight import human_bytes
        self.name = 'Transferring'
        self._set('phase', self.name)
        total = self.total if self.total is not None else total
        done = self.offset + done
        self.done = done
        remaining = max(0, total - done)
        eta = remaining / speed if speed > 0 else -1
        percent = min(100.0, done * 100.0 / total) if total else 0.0
        self._native('workItemSetCustomState', 'Transferring')
        self._native('workItemSetCookPercent', min(99.0, percent))
        for name, value in (('bytes_done', int(done)), ('bytes_total', int(total)),
                            ('speed_mib_s', speed / 2**20), ('eta_seconds', eta),
                            ('percent', percent)):
            self._set(name, value)
        self._set('progress', '{} / {} · {:.0f}% · {:.1f} MiB/s · {}'.format(
            human_bytes(done), human_bytes(total), percent, speed / 2**20,
            'ETA {:.0f}s'.format(eta) if eta >= 0 else 'ETA unknown'))

    def finish(self):
        self.phase('Complete')
        self._native('workItemSetCookPercent', 100.0)
        self._set('percent', 100)
        self._set('eta_seconds', 0)
        self._set('current_files', '')
        self._set('bytes_done', int(max(self.done, self.offset)))


def notify(callback, method, *args, **kwargs):
    function = getattr(callback, method, None)
    if function:
        function(*args, **kwargs)
