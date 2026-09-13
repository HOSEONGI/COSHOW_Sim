"""Test read-only stage recording, analysis, and launcher input boundaries."""
import asyncio
import csv
import importlib
import io
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    path = ROOT / 'dashboard/tests' / (name + '.py')
    assert path.exists(), 'Missing stage tool: ' + str(path)
    return importlib.import_module('dashboard.tests.' + name)


def messages():
    hello = {'type': 'hello', 'drones': ['reader'], 'limos': [],
             'freshness_s': {'pose': .8, 'odom': 1., 'camera': 2.}}
    state = {'type': 'state', 'run': {'state': 'RUNNING'},
             'mission': {'phase': 'observe'},
             'events': [{'t': 100., 'level': 'info', 'text': 'BT started'}],
             'robots': {'reader': {'kind': 'drone', 'role': 'reader', 'fleet_id': 'D1',
                 'pose': {'x': 1., 'y': 2., 'z': .3, 'yaw': .4}, 'pose_age': .2,
                 'status_age': .1, 'battery_v': 4., 'rssi': 60, 'armed': False,
                 'camera': {'fps': 8., 'stream_ok': True, 'frame_age': .1}},
                 'reserve': {'kind': 'drone', 'role': None, 'fleet_id': 'D2',
                             'pose': None, 'pose_age': None, 'armed': False}}}
    return hello, state


def test_recorder_deduplicates_transitions_and_events_but_records_every_state():
    record, poses = io.StringIO(), io.StringIO()
    recorder = module('stage_probe').ProbeRecorder(record=record, poses=poses, fleet=True)
    hello, state = messages()
    recorder.ingest(hello, received_at='2026-09-13T00:00:00+00:00', monotonic_s=10.)
    first = recorder.ingest(state, received_at='2026-09-13T00:00:01+00:00', monotonic_s=11.)
    second = recorder.ingest(state, received_at='2026-09-13T00:00:02+00:00', monotonic_s=12.)
    assert sum('PHASE' in line for line in first) == 1
    assert sum('EVENT' in line for line in first) == 1
    assert second == []
    rows = [json.loads(line) for line in record.getvalue().splitlines()]
    assert len(rows) == 3 and rows[-1]['message'] == state
    assert rows[-1]['monotonic_s'] == 12.
    data = list(csv.DictReader(io.StringIO(poses.getvalue())))
    assert len(data) == 4
    assert data[0]['robot'] == 'reader' and data[0]['pose_fresh'] == 'yes'
    assert data[1]['robot'] == 'reserve' and data[1]['pose_fresh'] == 'no'
    assert data[1]['z'] == ''


def test_fleet_table_uses_hello_roles_and_reports_camera_receipts_and_stale_poses():
    recorder = module('stage_probe').ProbeRecorder(fleet=True)
    hello, state = messages()
    recorder.ingest(hello, monotonic_s=10.)
    state['robots']['reader']['pose_age'] = 1.2
    recorder.ingest(state, monotonic_s=10.)
    for _ in range(25):
        recorder.frame(bytes([0]) + b'jpeg')
    table = recorder.table(monotonic_s=15.)
    assert 'reader' in table and 'reserve' in table
    assert 'camera_rx_fps' in table and '5.00' in table
    assert 'STALE' in table
    recorder.fleet = False
    assert 'reserve' not in recorder.table(monotonic_s=20.)


def test_analyzer_reports_phase_and_estop_timeline_without_duplicate_events():
    packets = []
    hello, state = messages()
    for tick, phase, run, event in [
        (0, 'observe', 'RUNNING', 'BT started'),
        (10, 'capture', 'RUNNING', 'BT started'),
        (11, 'capture', 'LANDING', 'BT SIGINT sent once'),
        (12, 'capture', 'LANDING', 'land sent 1/1'),
        (13, 'capture', 'ABORTED', 'vehicle 취소 미확인'),
        (20, None, 'IDLE', 'reset'),
        (100, None, 'READY', 'preflight ready'),
    ]:
        value = dict(state, mission={'phase': phase}, run={'state': run},
                     events=[{'t': 100. + tick, 'level': 'info', 'text': event}])
        packets.append(json.dumps({'received_at': '2026-09-13T00:00:00+00:00',
                                   'monotonic_s': float(tick), 'message': value}))
    packets.append(packets[-1])
    result = module('analyze_stage').analyze_lines(packets)
    assert [row['detail'] for row in result['timeline'] if row['kind'] == 'phase'] == ['observe', 'capture']
    assert result['recovery_to_ready_seconds'] == [80.]
    assert len([row for row in result['timeline'] if row['detail'] == 'BT SIGINT sent once']) == 1
    assert 'LANDING' in module('analyze_stage').render_report(result)


def test_analyzer_rejects_corrupt_json_with_line_number():
    with pytest.raises(ValueError, match='line 2'):
        module('analyze_stage').analyze_lines(['{}', 'broken'])


def test_stage_websocket_probe_never_sends_admin_commands():
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    async def exercise():
        incoming, roles = [], []
        hello, state = messages()
        async def websocket(request):
            roles.append(request.query.get('role'))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(hello)
            await socket.send_json(state)
            await socket.send_bytes(b'\x00jpeg')
            async for item in socket:
                incoming.append(item)
            return socket
        app = web.Application()
        app.router.add_get('/ws', websocket)
        async with TestServer(app) as server:
            tool = module('stage_probe')
            output = []
            recorder = tool.ProbeRecorder(fleet=True)
            await tool.collect(str(server.make_url('/')), recorder, seconds=.12, interval=.05, emit=output.append)
        assert roles == ['admin'] and not incoming
        assert any('PHASE' in line for line in output)
        assert any('camera_rx_fps' in line for line in output)
    asyncio.run(exercise())


@pytest.mark.parametrize('script', ['install.sh', 'run.sh', 'kiosk.sh'])
def test_launch_scripts_parse_as_bash(script):
    path = ROOT / 'dashboard' / script
    assert path.exists(), 'Missing launch script: ' + script
    subprocess.run(['bash', '-n', str(path)], check=True)


@pytest.mark.parametrize('scale', ['nan', 'inf', '.69', '1.61', 'invalid'])
def test_kiosk_rejects_invalid_scale_before_launching(scale):
    path = ROOT / 'dashboard/kiosk.sh'
    assert path.exists(), 'Missing kiosk script'
    result = subprocess.run(['bash', str(path), '--dry-run'], text=True, capture_output=True,
                            env=dict(os.environ, VISITOR_SCALE=scale, XDG_SESSION_TYPE='x11', DISPLAY=':99'))
    assert result.returncode != 0 and 'VISITOR_SCALE' in result.stderr


def test_kiosk_rejects_wayland_with_operator_action():
    path = ROOT / 'dashboard/kiosk.sh'
    assert path.exists(), 'Missing kiosk script'
    result = subprocess.run(['bash', str(path), '--dry-run'], text=True, capture_output=True,
                            env=dict(os.environ, VISITOR_SCALE='1', XDG_SESSION_TYPE='wayland'))
    assert result.returncode != 0 and 'Ubuntu on Xorg' in result.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason='root rejection is exercised by the Linux Docker harness')
def test_installer_refuses_whole_script_sudo_before_any_changes():
    result = subprocess.run(['bash', str(ROOT / 'dashboard/install.sh')], text=True, capture_output=True)
    assert result.returncode == 2 and 'without sudo' in result.stderr
