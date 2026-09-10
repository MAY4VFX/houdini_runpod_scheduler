"""Offline Qt smoke of File Review, using disposable files only."""
import argparse
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets
from rpfarm import deps, preflight as pf, file_review as fr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with tempfile.TemporaryDirectory(prefix='rpfarm-review-fixture-') as tmp:
        root = pathlib.Path(tmp)
        for name in ('scene.hip', 'textures/wood.rat', 'textures/metal.rat',
                     'cache/sim.0001.bgeo.sc'):
            p = root / name
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(b'x' * 100)
        rows, _ = deps.plan_refs([str(root)])
        review = fr.FileReview(rows, pf.build_tree(rows), str(root),
                               '/workspace/projects/artist/airship', {
            'textures/wood.rat': (100, (root / 'textures/wood.rat').stat().st_mtime),
            'textures/metal.rat': (99, 0), 'render/beauty.001.exr': (200, 0)},
            intent='preview')
        checked = {str(root / 'scene.hip'), str(root / 'textures/wood.rat')}
        dialog = fr.build_dialog(review, [], checked)
        dialog.show()
        app.processEvents()
        assert dialog.rpfarm_remote_model.rowCount() > 0
        assert any(b.text() == 'Save Selection' for b in dialog.findChildren(QtWidgets.QPushButton))
        assert dialog.rpfarm_checked() == checked
        dialog.rpfarm_set_checked({str(root / 'scene.hip')})
        assert dialog.rpfarm_checked() == {str(root / 'scene.hip')}
        dialog.rpfarm_set_checked(checked)
        dialog.rpfarm_view.expandAll()
        dialog.rpfarm_remote_view.expandAll()
        app.processEvents()
        dialog.grab().save(args.output)
        dialog.close()
    print('Qt: two trees, preview action, selection and screenshot passed')


if __name__ == '__main__':
    main()
