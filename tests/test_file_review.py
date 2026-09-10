import os
import types
import pytest

from rpfarm import deps, preflight, file_review


def review(tmp_path):
    a = tmp_path / 'same.rat'
    b = tmp_path / 'local.rat'
    a.write_bytes(b'abc')
    b.write_bytes(b'x')
    rows, _ = deps.plan_refs([str(a), str(b)])
    root = '/workspace/projects/artist/shot'
    index = {'same.rat': (3, a.stat().st_mtime), 'old.exr': (10, 0)}
    return file_review.FileReview(rows, preflight.build_tree(rows), str(tmp_path), root, index), a, b


def test_both_trees_distinguish_same_local_and_remote_only(tmp_path):
    model, a, b = review(tmp_path)
    local = {n.path: n.farm_state for n in preflight.leaves(model.roots)}
    remote = {n.path: n.farm_state for n in preflight.leaves(model.remote_roots())}
    assert local[str(a)] == 'same'
    assert local[str(b)] == 'missing'
    assert remote[model.remote_project + '/same.rat'] == 'same'
    assert remote[model.remote_project + '/old.exr'] == 'remote_only'


def test_unknown_is_never_shown_as_missing(tmp_path):
    model, _, _ = review(tmp_path)
    model.index = None
    model.annotate()
    assert {n.farm_state for n in preflight.leaves(model.roots)} == {'unknown'}
    assert model.remote_roots() == []


def test_delete_only_expands_displayed_files_not_entire_directory(tmp_path):
    model, a, b = review(tmp_path)
    unlisted = tmp_path / 'private.txt'
    unlisted.write_text('keep')
    assert set(model.delete_targets('local', [str(tmp_path)])) == {str(a), str(b)}
    with pytest.raises(ValueError):
        model.delete_targets('local', ['/'])
    with pytest.raises(ValueError):
        model.delete_targets('farm', ['/workspace'])


def test_preview_and_cook_have_different_actions():
    assert file_review.accept_label('preview') == 'Save Selection'
    assert file_review.accept_label('cook') == 'Continue Upload'


def test_plan_explains_one_package_and_can_expand_to_file_items(tmp_path):
    model, a, b = review(tmp_path)
    plan = model.upload_plan({str(a), str(b)})
    assert len(plan) == 1 and len(plan[0]['files']) == 2
    model.grouping = 'files'
    plan = model.upload_plan({str(a), str(b)})
    assert len(plan) == 2
    assert all(len(it['files']) == 1 for it in plan)


def test_refresh_after_trash_removes_file_from_upload_plan(tmp_path):
    model, a, b = review(tmp_path)
    os.unlink(b)  # fixture only, simulates the user's confirmed Trash action
    model.refresh_local()
    assert [n.path for n in preflight.leaves(model.roots)] == [str(a)]
    assert preflight.selected_paths(model.rows, {str(a), str(b)}) == [str(a)]


def test_active_farm_project_cannot_be_deleted(tmp_path):
    model, _, _ = review(tmp_path)
    model.cfg = types.SimpleNamespace(user='artist')
    model.api = types.SimpleNamespace(list_pods=lambda: [{
        'desiredStatus': 'RUNNING',
        'env': {'RPFARM_USER': 'artist', 'RPFARM_PROJECT': 'shot'}}])
    with pytest.raises(ValueError, match='active or protected'):
        model.delete('farm', [model.remote_project + '/old.exr'])


def test_another_users_farm_project_cannot_be_deleted(tmp_path):
    model, _, _ = review(tmp_path)
    model.cfg = types.SimpleNamespace(user='someone_else')
    model.api = types.SimpleNamespace(list_pods=lambda: [])
    with pytest.raises(ValueError, match='active or protected'):
        model.delete('farm', [model.remote_project + '/old.exr'])


def test_remote_index_cannot_introduce_a_path_outside_the_project(tmp_path):
    model, _, _ = review(tmp_path)
    model.index['../../houdini/file'] = (100, 0)
    assert all(n.path.startswith(model.remote_project + '/')
               for n in preflight.leaves(model.remote_roots()))
