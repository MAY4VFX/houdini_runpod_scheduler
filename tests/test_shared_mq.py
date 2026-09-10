from rpfarm.mq import ensure_shared_mq


def test_two_cooks_share_one_daemon(tmp_path):
    shared = tmp_path / 'shared.txt'
    started = []
    def spawn(command, **kwargs):
        started.append(command)
        shared.write_text('PDG_MQ 127.0.0.1 4440 4440 4442\n')
    for cook in ('one', 'two'):
        result = ensure_shared_mq(str(tmp_path / (cook + '.txt')), str(shared),
            str(tmp_path / 'mq.log'), str(tmp_path / 'mq.lock'),
            spawn=spawn, probe=lambda: bool(started))
        assert result.startswith('PDG_MQ ')
    assert len(started) == 1
    assert started[0][:3] == ['mqserver', '-p', '4440']
