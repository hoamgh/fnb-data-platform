import json

import pytest

from scripts import google_live_workers as workers


@pytest.mark.parametrize('worker_id', ['worker-01', 'worker-02'])
@pytest.mark.parametrize('previous', [None, '', 'existing-value'])
@pytest.mark.parametrize('failure', [None, 'constructor', 'timeout_config', 'lock'])
def test_startup_diagnostics_preserve_browser_behavior(tmp_path, monkeypatch, failure, previous, worker_id):
    for key in ('SE_DEBUG', 'CHROME_LOG_FILE'):
        if previous is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, previous)

    def assert_restored():
        for key in ('SE_DEBUG', 'CHROME_LOG_FILE'):
            assert workers.os.environ.get(key) == previous

    directory = tmp_path/worker_id
    directory.mkdir()
    profile = tmp_path/'private-profile'
    armed, cancelled = [], []
    monkeypatch.setattr(workers.faulthandler, 'dump_traceback_later',
                        lambda delay, **kw: armed.append((delay, kw)))
    monkeypatch.setattr(workers.faulthandler, 'cancel_dump_traceback_later',
                        lambda: cancelled.append(True))
    problem = ValueError('original failure')
    released, instances = [], []

    def events():
        return [json.loads(line) for line in (directory/'driver-startup.jsonl').read_text().splitlines()]

    class Lock:
        def acquire(self, *, timeout):
            assert timeout == 60
            assert events()[-1]['event'] == 'DRIVER_LOCK_WAIT'
            return failure != 'lock'

        def release(self):
            released.append(True)

    class Driver:
        def __init__(self, **options):
            assert workers.os.environ['SE_DEBUG'] == '1'
            assert workers.os.environ['CHROME_LOG_FILE'] == str((directory/'chrome.log').resolve())
            network = json.loads((directory/'driver-network.json').read_text())
            assert options == {'uc': True, 'user_data_dir': str(profile),
                               'chromium_arg': f"--remote-debugging-port={network['selected_debug_port']}"}
            assert network['worker_id'] == worker_id
            assert network['selected_debug_port'] not in network['existing_listening_ports']
            assert events()[-1]['event'] == 'DRIVER_CALL_START'
            if failure == 'constructor':
                raise problem
            instances.append(self)
            self.closed = False

        def set_page_load_timeout(self, seconds):
            assert_restored()
            assert seconds == 30
            if failure == 'timeout_config':
                raise problem

        def set_script_timeout(self, seconds):
            assert seconds == 15

        def quit(self):
            self.closed = True

    startups = []
    if failure:
        with pytest.raises(RuntimeError if failure == 'lock' else ValueError) as caught:
            workers.start_diagnosed_driver(Driver, Lock(), profile, directory, {'uc': True}, startups, startup_debug=True)
        if failure != 'lock':
            assert caught.value is problem
        else:
            assert str(caught.value) == 'Driver startup lock timeout'
        assert startups == []
    else:
        assert workers.start_diagnosed_driver(Driver, Lock(), profile, directory, {'uc': True}, startups, startup_debug=True) is instances[0]
        assert len(startups) == 1
    assert_restored()
    expected = ['DRIVER_LOCK_WAIT']
    if failure != 'lock':
        expected += ['DRIVER_LOCK_ACQUIRED', 'DRIVER_CALL_START']
        if failure != 'constructor':
            expected += ['DRIVER_CALL_RETURN', 'DRIVER_TIMEOUT_CONFIG_START']
            if failure != 'timeout_config':
                expected += ['DRIVER_TIMEOUT_CONFIG_END']
        expected += ['DRIVER_LOCK_RELEASED']
    records = events()
    assert [r['event'] for r in records] == expected
    assert released == ([] if failure == 'lock' else [True])
    assert armed[0][0] == 90
    assert armed[0][1]['repeat'] is False and armed[0][1]['exit'] is False
    assert cancelled == [True]
    assert armed[0][1]['file'].closed
    assert all(r['worker_id'] == worker_id and r['pid'] == workers.os.getpid() for r in records)
    assert all(r['timestamp'].endswith('+00:00') and len(r['profile_id']) == 16 for r in records)
    assert [r['elapsed_ms'] for r in records] == sorted(r['elapsed_ms'] for r in records)
    assert str(profile) not in (directory/'driver-startup.jsonl').read_text()
    if failure == 'timeout_config':
        assert instances[0].closed


def test_debug_port_avoids_existing_listener(tmp_path):
    import socket
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        busy_port = occupied.getsockname()[1]
        chosen = workers.choose_debug_port(tmp_path)
        assert chosen != busy_port
        evidence = json.loads((tmp_path/'driver-network.json').read_text())
        assert busy_port in evidence['existing_listening_ports']
        with socket.socket() as available:
            available.bind(('127.0.0.1', chosen))

def test_verbose_startup_is_opt_in(tmp_path, monkeypatch):
    from contextlib import contextmanager
    monkeypatch.delenv('SE_DEBUG', raising=False)
    monkeypatch.delenv('CHROME_LOG_FILE', raising=False)

    @contextmanager
    def quiet(*args):
        yield lambda event: None

    monkeypatch.setattr(workers, 'driver_startup_diagnostics', quiet)
    monkeypatch.setattr(workers, 'choose_debug_port', lambda directory: 54321)

    class Lock:
        def acquire(self, **kwargs): return True
        def release(self): pass

    class Driver:
        def __init__(self, **options):
            assert 'SE_DEBUG' not in workers.os.environ
            assert 'CHROME_LOG_FILE' not in workers.os.environ
            assert options['chromium_arg'] == '--remote-debugging-port=54321'
        def set_page_load_timeout(self, value): assert value == 30
        def set_script_timeout(self, value): assert value == 15

    assert isinstance(workers.start_diagnosed_driver(Driver, Lock(), tmp_path, tmp_path,
                                                    {'uc': True}, []), Driver)
