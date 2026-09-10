"""Local/farm comparison and actions used by the upload preflight window.

Only the explicit delete action mutates storage. Merely opening or refreshing
the window never rents a machine. The local tree is the scene's references,
not a recursive browser of an artist's entire $JOB (which may be their home).
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex

from . import deps, preflight as pf, sync


def accept_label(intent):
    return 'Save Selection' if intent == 'preview' else 'Continue Upload'


class FileReview:
    def __init__(self, rows, roots, job_dir, remote_project, index=None,
                 cfg=None, api=None, intent='cook'):
        self.rows, self.roots = rows, roots
        self.job_dir = job_dir
        self.remote_project = remote_project
        self.index = index
        self.cfg, self.api = cfg, api
        self.intent = intent
        self.package_gb = 1.5
        self.grouping = 'packages'
        self.note = ''
        self.annotate()

    def upload_plan(self, checked):
        from . import packages
        user, project = self.remote_project.rstrip('/').split('/')[-2:]
        return packages.build_upload_items('deps', self.job_dir, user, project, [],
                                           pf.selected_paths(self.rows, checked), self.package_gb,
                                           grouping=self.grouping)

    def annotate(self):
        self.pairs = pf.farm_pairs([n.path for n in pf.leaves(self.roots)],
                                   self.job_dir, self.remote_project)
        pf.annotate_farm_state(self.roots, self.pairs, self.index, self.remote_project)

    def remote_roots(self):
        if self.index is None:
            return []
        states = {self.pairs.get(n.path): n.farm_state for n in pf.leaves(self.roots)}
        rows = []
        for rel, (size, _mtime) in self.index.items():
            path = posixpath.normpath(posixpath.join(self.remote_project, rel))
            if not path.startswith(self.remote_project.rstrip('/') + '/'):
                continue
            rows.append(deps.PlanRow(path=path, kind='file', files=1, bytes=size))
        roots = pf.build_tree(rows)
        def annotate(n):
            if n.is_leaf:
                n.farm_state = states.get(n.path, 'remote_only')
            else:
                values = {annotate(c) for c in n.children}
                n.farm_state = values.pop() if len(values) == 1 else 'mixed'
            return n.farm_state
        for n in roots:
            annotate(n)
        return roots

    def refresh_local(self):
        # Re-weigh only the references already in the window. Never traverse
        # a broader directory because a user clicked a folder row.
        rows = []
        for n in pf.leaves(self.roots):
            if os.path.isfile(n.path):
                rows.append(deps.PlanRow(path=n.path, kind='file', files=1,
                                         bytes=os.path.getsize(n.path), source=n.source))
            elif n.kind == 'dir' and os.path.isdir(n.path):
                found, _ = deps.plan_refs([n.path], source=n.source)
                rows.extend(found)
        self.rows[:] = rows
        self.roots[:] = pf.build_tree(rows)
        self.annotate()

    def refresh(self):
        if self.cfg and self.api:
            sync.clear_shared_remote_index()
            self.index = pf.fetch_farm_index(self.cfg, self.api, self.remote_project)
        self.refresh_local()

    def delete_targets(self, side, selected):
        roots = self.roots if side == 'local' else self.remote_roots()
        nodes = {}
        def visit(n):
            nodes[n.path] = n
            for c in n.children:
                visit(c)
        for n in roots:
            visit(n)
        paths = set()
        for path in selected:
            if path not in nodes:
                raise ValueError('Select files inside the displayed {} tree.'.format(side))
            for n in pf.leaves([nodes[path]]):
                if n.kind != 'file':
                    raise ValueError('Expand/list the directory before deleting its files.')
                paths.add(n.path)
        return sorted(paths)

    def delete(self, side, paths):
        # Revalidate the explicit leaf list at the action boundary.
        paths = self.delete_targets(side, paths)
        if side == 'local':
            from PySide6 import QtCore
            try:
                import hou
                current_hip = os.path.realpath(hou.hipFile.path())
            except ImportError:
                current_hip = ''
            if any(os.path.realpath(p) == current_hip for p in paths):
                raise ValueError('The currently open scene cannot be moved to Trash.')
            gone, refused = [], []
            for path in paths:
                result = QtCore.QFile.moveToTrash(path)
                ok = result[0] if isinstance(result, tuple) else result
                (gone if ok else refused).append(path)
            self.note = '{} local file(s) moved to Trash.'.format(len(gone))
            if refused:
                self.note += ' Could not move: ' + ', '.join(refused)
        else:
            if not self.cfg or not self.api or self.index is None:
                raise ValueError('Farm state is unavailable. Refresh before deleting.')
            from . import pods, volume
            from .worker_client import WorkerClient
            from . import config
            active = set()
            for p in self.api.list_pods():
                env = p.get('env') or {}
                if p.get('desiredStatus') == 'RUNNING' and env.get('RPFARM_PROJECT'):
                    active.add('{}/{}'.format(env.get('RPFARM_USER'), env['RPFARM_PROJECT']))
            if any(volume.is_locked(p, self.cfg.user, active) for p in paths):
                raise ValueError('An active or protected project cannot be deleted.')
            pod = pods.find_running_sync_pod(self.api, self.cfg)
            if not pod:
                raise ValueError('No sync pod is running. Nothing was deleted.')
            client = WorkerClient(pod['id'], config.session_token())
            health = client.health()
            if not health or health.get('transfers') is None or health.get('transfers'):
                raise ValueError('A transfer is active or its state is unknown. Try again later.')
            command = 'python3 /opt/rpfarm/housekeeping.py rm-paths ' + ' '.join(
                shlex.quote(p) for p in paths)
            result = client.exec_wait(command, deadline_s=300)
            if result.get('exit_code'):
                raise ValueError(result.get('stderr') or 'Farm deletion failed.')
            data = json.loads(result.get('stdout') or '{}')
            self.note = '{} farm file(s) deleted permanently.'.format(len(data.get('deleted', [])))
            if data.get('refused'):
                self.note += ' Kept: ' + '; '.join('{}: {}'.format(p['path'], p['error'])
                                                   for p in data['refused'])
        self.refresh()
        return self.note


LABELS = dict(pf.FARM_STATE_LABELS, same='Identical',
              differs='Different', missing='Local only', unknown='Unknown',
              remote_only='Not referenced locally', mixed='Mixed')


def build_dialog(review, missing, checked, parent=None, mbps=None):
    from PySide6 import QtCore, QtGui, QtWidgets

    dialog = pf.build_dialog(review.roots, missing, checked,
                             title='RunPodFarm — File Review', parent=parent, mbps=mbps,
                             accept_label=accept_label(review.intent),
                             allow_empty=review.intent == 'preview')
    layout = dialog.layout()
    local_view = dialog.rpfarm_view
    local_view.setColumnWidth(0, 270)
    local_view.setColumnWidth(1, 70)
    local_view.setColumnWidth(2, 70)
    local_view.setColumnHidden(3, True)
    local_view.setColumnWidth(4, 170)
    layout.removeWidget(local_view)
    splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
    def panel(title, view):
        w = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        label = QtWidgets.QLabel(title)
        label.setWordWrap(True)
        col.addWidget(label)
        col.addWidget(view, 1)
        splitter.addWidget(w)
    panel('LOCAL REFERENCES\n' + review.job_dir, local_view)
    hqt = pf.houdini_qt()
    remote_view = hqt.TreeView() if hqt and hasattr(hqt, 'TreeView') else QtWidgets.QTreeView()
    remote_view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    remote_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    remote_view.setUniformRowHeights(True)
    model = QtGui.QStandardItemModel(remote_view)
    remote_view.setModel(model)
    panel('FARM\n' + review.remote_project, remote_view)
    layout.insertWidget(1, splitter, 1)
    splitter.setSizes([550, 550])
    legend = QtWidgets.QLabel('Green: identical size / modification time · Amber: different · '
                              'Local only / Not referenced locally · Unknown: farm not available\n'
                              'Checkboxes select uploads. Highlight rows to delete files.')
    legend.setWordWrap(True)
    layout.insertWidget(2, legend)
    note = QtWidgets.QLabel()
    note.setWordWrap(True)
    layout.insertWidget(3, note)

    plan_row = QtWidgets.QHBoxLayout()
    grouping = QtWidgets.QComboBox()
    grouping.addItem('Packages by size', 'packages')
    grouping.addItem('One work item per file', 'files')
    grouping.setCurrentIndex(1 if review.grouping == 'files' else 0)
    limit = QtWidgets.QDoubleSpinBox()
    limit.setRange(0.01, 1024)
    limit.setDecimals(2)
    limit.setSuffix(' GiB / item')
    limit.setValue(review.package_gb)
    packages = QtWidgets.QComboBox()
    packages.setMinimumContentsLength(24)
    packages.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    plan_row.addWidget(grouping)
    plan_row.addWidget(limit)
    plan_row.addWidget(packages, 1)
    layout.insertLayout(1, plan_row)

    def update_plan(checked=None):
        review.package_gb = limit.value()
        review.grouping = grouping.currentData()
        limit.setEnabled(review.grouping == 'packages')
        checked = dialog.rpfarm_checked() if checked is None else checked
        plan = review.upload_plan(checked)
        packages.clear()
        for it in plan:
            packages.addItem('Item {} / {} · {} files · {}'.format(
                it['index'] + 1, len(plan), len(it['files']), pf.human_bytes(it['bytes'])))
            packages.setItemData(packages.count() - 1, '\n'.join(f[0] for f in it['files']),
                                 QtCore.Qt.ToolTipRole)
        if not plan:
            packages.addItem('No files selected')
        packages.setToolTip('Each work item contains these files before the farm comparison. '
                            'Unchanged files are skipped. Hover an item to see its full file list.')

    dialog.rpfarm_selection_changed = update_plan
    grouping.currentIndexChanged.connect(lambda _i: update_plan())
    limit.valueChanged.connect(lambda _v: update_plan())

    def colour_row(item, state):
        if state not in ('same', 'differs'):
            return
        brush = QtGui.QBrush(QtGui.QColor(52, 145, 77, 65) if state == 'same'
                            else QtGui.QColor(207, 148, 44, 65))
        parent_item = item.parent() or item.model().invisibleRootItem()
        for c in range(parent_item.columnCount()):
            cell = parent_item.child(item.row(), c)
            if cell:
                cell.setBackground(brush)

    def decorate_local():
        dialog.rpfarm_model.blockSignals(True)
        states = {}
        def collect(n):
            states[n.path] = n.farm_state
            for c in n.children:
                collect(c)
        for n in review.roots:
            collect(n)
        def visit(parent_item):
            for r in range(parent_item.rowCount()):
                item = parent_item.child(r)
                state = states.get(item.data(QtCore.Qt.UserRole + 1), '')
                cell = parent_item.child(r, 4)
                if cell:
                    cell.setText(LABELS.get(state, state))
                colour_row(item, state)
                visit(item)
        visit(dialog.rpfarm_model.invisibleRootItem())
        dialog.rpfarm_model.blockSignals(False)

    def render_remote():
        model.clear()
        model.setHorizontalHeaderLabels(['File', 'Size', 'State'])
        def add(n, parent_item):
            item = QtGui.QStandardItem(n.name)
            item.setData(n.path, QtCore.Qt.UserRole + 1)
            item.setToolTip(n.path)
            cells = [item, QtGui.QStandardItem(pf.human_bytes(n.bytes)),
                     QtGui.QStandardItem(LABELS.get(n.farm_state, n.farm_state))]
            for cell in cells:
                cell.setEditable(False)
            parent_item.appendRow(cells)
            colour_row(item, n.farm_state)
            for child in n.children:
                add(child, item)
        for root in review.remote_roots():
            add(root, model.invisibleRootItem())
        remote_view.setColumnWidth(0, 280)
        remote_view.setColumnWidth(1, 90)
        remote_view.expandToDepth(0)
        note.setText(review.note or ('Farm state unknown — no running sync pod or listing unavailable.'
                                     if review.index is None else ''))
        decorate_local()

    def redraw():
        dialog.rpfarm_rebuild_tree()
        render_remote()

    actions = QtWidgets.QHBoxLayout()
    refresh = QtWidgets.QPushButton('Refresh Comparison')
    reset = QtWidgets.QPushButton('Reset Upload Selection')
    local_delete = QtWidgets.QPushButton('Move Selected Local Files to Trash…')
    remote_delete = QtWidgets.QPushButton('Delete Selected Farm Files…')
    for button in (refresh, reset, local_delete, remote_delete):
        actions.addWidget(button)
    layout.insertLayout(4, actions)

    def refresh_clicked():
        try:
            review.refresh()
            redraw()
        except Exception as exc:
            note.setText(str(exc))

    def delete_clicked(side):
        view = local_view if side == 'local' else remote_view
        selected = [i.data(QtCore.Qt.UserRole + 1) for i in view.selectionModel().selectedRows()]
        try:
            paths = review.delete_targets(side, selected)
            if not paths:
                note.setText('Select rows in the {} tree first.'.format(side))
                return
            action = 'Move to Trash' if side == 'local' else 'Delete Permanently'
            question = QtWidgets.QMessageBox(dialog)
            question.setWindowTitle(action)
            question.setText('{} {} {} file(s)?'.format(action, len(paths), side))
            question.setInformativeText('These are the selected rows, not the upload checkboxes. '
                                        'This action happens now; Cancel in File Review will not undo it.')
            question.setDetailedText('\n'.join(paths))
            go = question.addButton(action, QtWidgets.QMessageBox.DestructiveRole)
            cancel = question.addButton(QtWidgets.QMessageBox.Cancel)
            question.setDefaultButton(cancel)
            question.exec()
            if question.clickedButton() is not go:
                return
            review.delete(side, paths)
            redraw()
        except Exception as exc:
            note.setText(str(exc))

    refresh.clicked.connect(refresh_clicked)
    reset.clicked.connect(lambda: dialog.rpfarm_set_checked(
        {n.path for n in pf.leaves(review.roots) if n.source != 'output'}))
    local_delete.clicked.connect(lambda: delete_clicked('local'))
    remote_delete.clicked.connect(lambda: delete_clicked('farm'))
    dialog.rpfarm_remote_view = remote_view
    dialog.rpfarm_remote_model = model
    dialog.resize(1250, 720)
    render_remote()
    update_plan()
    return dialog
