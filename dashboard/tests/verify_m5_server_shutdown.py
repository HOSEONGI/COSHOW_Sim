#!/usr/bin/env python3
"""Probe production aiohttp SIGINT cleanup with real local ROS services.

Requires ROS_DOMAIN_ID=90 and ROS_LOCALHOST_ONLY=1. No radio, camera, navigation
action, or real BT is started. Only readiness checklist evaluation is replaced
by an empty fixture checklist; public admin commands, ROS receipts, process
ownership, Runner, Dashboard shutdown hooks and ROSIO are the production code.
Safe child processes and all run/log/roster output live in a temporary directory.

The negative control --regression-late-cleanup removes the early on_shutdown
hook in the test application only. An open admin socket then reproduces the
old aiohttp cleanup stall; the probe fails after six seconds and reaps only its
own verified test children. Production source files are never rewritten.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import shlex
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dashboard.config import load_config

SCRIPT = Path(__file__).resolve()
LAND_DURATION = .25
EXIT_BOUND = 6


def append(path, kind, **data):
    record = dict(kind=kind, at=time.monotonic(), **data)
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    return record


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def fixture_config(root, prefix, scenario):
    cfg = load_config(mock=True)  # In-memory inventory and flags only.
    cfg.mock = False
    cfg.bt['coshow']['durations']['land'] = LAND_DURATION
    cfg.raw['land']['height'] = 0.0
    cfg.raw['topics'] = {'pose_template': prefix + '/{cf}/pose',
                         'preflight_ready': prefix + '/ready',
                         'mission_state': prefix + '/mission'}
    cfg.raw['types']['topics'] = {key: cfg.raw['types']['topics'][key] for key in cfg.raw['topics']}
    cfg.raw['services'] = {'land_template': prefix + '/{cf}/land',
                           'arm_template': prefix + '/{cf}/arm'}
    cfg.raw['actions'] = {'nav_template': prefix + '/{limo}/navigate'}
    cfg.raw['roster_file'] = str(root / 'dashboard/run/roster.yaml')
    cfg.bt_cwd = root / 'bt'
    cfg.bt_cwd.mkdir(parents=True, exist_ok=True)
    for metadata in cfg.robots.values():
        metadata['ip'] = '127.0.0.1'
    for kind in ('bt', 'preflight'):
        cfg.raw['commands'][kind] = shlex.join([
            sys.executable, str(SCRIPT), '--child', kind, '--root', str(root),
            '--scenario', scenario, '--config', str(cfg.bt_path)])
    cfg.raw['commands']['env'] = {}
    return cfg


def safe_child(args):
    def stopped(number, frame):
        append(args.root / 'children.jsonl', 'signal', child=args.child,
               pid=os.getpid(), signal=number)
        if args.child == 'preflight' or args.scenario == 'early-exit':
            raise SystemExit(0)

    signal.signal(signal.SIGINT, stopped)
    append(args.root / 'children.jsonl', 'started', child=args.child,
           pid=os.getpid(), sid=os.getsid(0), core_limit=list(resource.getrlimit(resource.RLIMIT_CORE)))
    while True:
        time.sleep(.1)


def server_child(args):
    from aiohttp import web
    from dashboard import runner
    from dashboard.server import Dashboard

    runner.HERE = args.root / 'dashboard'
    cfg = fixture_config(args.root, args.prefix, args.scenario)
    dashboard = Dashboard(cfg, mock=False)
    app = dashboard.app()

    async def fixture_ready(application):
        # This probe checks teardown, not radio/camera/BT environment preflight.
        # The ready=True transition itself still arrives over real DDS.
        dashboard.runner.state_callback = dashboard.store.snapshot
        deadline = time.monotonic() + 5
        while not all(client.service_is_ready() for client in dashboard.io.service_clients.values()):
            assert time.monotonic() < deadline, 'Local service discovery timed out'
            await asyncio.sleep(.02)
        assert len(dashboard.io.service_clients) == len(cfg.drones) * 2 + len(cfg.limos)
        print('FIXTURE: readiness checklist only; mock=False; production Dashboard/Runner/ROSIO', flush=True)

    async def shutdown_observed(application):
        append(args.root / 'server.jsonl', 'shutdown_complete',
               state=dashboard.store.snapshot(),
               children={name: dict(pid=child.pid, returncode=child.returncode,
                                    sigint_sent=child.sigint_sent)
                         for name, child in dashboard.runner.children.items()},
               ros_alive=dashboard.io.node is not None,
               open_sockets=sum(not slot.ws.closed for slot in dashboard.sockets))

    async def cleanup_observed(application):
        append(args.root / 'server.jsonl', 'cleanup_complete',
               ros_closed=dashboard.io.node is None,
               tasks_done=all(task.done() for task in dashboard.tasks))

    app.on_startup.append(fixture_ready)
    if args.regression_late_cleanup:
        app.on_shutdown.remove(dashboard.shutdown)
        print('NEGATIVE CONTROL: early on_shutdown hook removed in test application only', flush=True)
    app.on_shutdown.append(shutdown_observed)
    app.on_cleanup.append(cleanup_observed)
    web.run_app(app, host='127.0.0.1', port=args.port, print=None)


class LocalROS:
    def __init__(self, cfg, prefix):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rosidl_runtime_py.utilities import get_message, get_service
        from dashboard.ros_io import interface_specs, _qos

        self.cfg, self.requests, self.landed_at = cfg, [], {}
        self.context = Context()
        rclpy.init(args=[], context=self.context)
        self.node = rclpy.create_node('server_shutdown_probe_' + uuid.uuid4().hex[:8], context=self.context)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.publishers = []
        for spec in interface_specs(cfg)[0]:
            assert spec['name'].startswith(prefix + '/'), spec
            if spec['kind'] == 'topics':
                typename = get_message(spec['type'])
                publisher = self.node.create_publisher(typename, spec['name'], _qos(spec['channel']))
                self.publishers.append((spec['channel'], spec['robot'], typename, publisher))
            else:
                channel = spec['channel'] if spec['kind'] == 'services' else 'cancel'
                name = spec['name'] if channel != 'cancel' else spec['name'] + '/_action/cancel_goal'
                typename = get_service(spec['type'] if channel != 'cancel' else 'action_msgs/srv/CancelGoal')
                self.node.create_service(typename, name, self.callback(spec['robot'], channel))
        self.node.create_timer(.02, self.publish)
        self.thread = threading.Thread(target=self.spin, daemon=True)
        self.thread.start()

    def callback(self, robot, channel):
        def receive(request, response):
            at = time.monotonic()
            if channel == 'land':
                wire = dict(height=request.height, group_mask=request.group_mask,
                            sec=request.duration.sec, nanosec=request.duration.nanosec)
                self.landed_at[robot] = at + LAND_DURATION
            elif channel == 'arm':
                wire = dict(arm=request.arm)
            else:
                wire = dict(uuid=[int(value) for value in request.goal_info.goal_id.uuid],
                            sec=request.goal_info.stamp.sec, nanosec=request.goal_info.stamp.nanosec)
                response.return_code = response.ERROR_NONE
            with self.lock:
                self.requests.append(dict(kind='service', robot=robot, channel=channel, at=at, wire=wire))
            return response
        return receive

    def publish(self):
        now = time.monotonic()
        for channel, robot, typename, publisher in self.publishers:
            message = typename()
            if channel == 'pose':
                message.header.stamp = self.node.get_clock().now().to_msg()
                message.pose.position.z = 0.0 if now >= self.landed_at.get(robot, float('inf')) else .5
                message.pose.orientation.w = 1.0
            elif self.landed_at:
                continue  # End mission/ready emission when direct landing begins.
            elif channel == 'ready':
                message.data = True
            else:
                message.data = json.dumps(dict(phase='capture', led={self.cfg.drones[0]: 'green'},
                                               cmd={self.cfg.drones[0]: dict(kind='hover', goal=[0, 0, .5])}))
            publisher.publish(message)

    def spin(self):
        while not self.stop.is_set():
            self.executor.spin_once(timeout_sec=.02)

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.executor.shutdown(timeout_sec=2)
        self.node.destroy_node()
        self.context.shutdown()


async def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Probe condition timed out'
        await asyncio.sleep(.02)


def kill_verified_children(root):
    for event in read_records(root / 'children.jsonl'):
        if event['kind'] != 'started':
            continue
        pid = event['pid']
        try:
            argv = Path('/proc/{}/cmdline'.format(pid)).read_bytes().split(b'\0')
            if str(SCRIPT).encode() in argv and str(root).encode() in argv and b'--child' in argv:
                os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass


async def scenario(name, regression=False):
    import aiohttp

    with tempfile.TemporaryDirectory(prefix='coshow-server-shutdown-') as directory:
        root = Path(directory)
        prefix = '/shutdown_probe_' + uuid.uuid4().hex[:10]
        cfg = fixture_config(root, prefix, name)
        ros = LocalROS(cfg, prefix)
        with socket.socket() as free_port:
            free_port.bind(('127.0.0.1', 0))
            port = free_port.getsockname()[1]
        command = [sys.executable, str(SCRIPT), '--server-child', '--root', str(root),
                   '--prefix', prefix, '--scenario', name, '--port', str(port)]
        if regression:
            command.append('--regression-late-cleanup')
        process = reader = None
        states, seen_states = [], []
        try:
            with (root / 'stdout.log').open('w') as output:
                process = await asyncio.create_subprocess_exec(*command, stdout=output,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                    deadline = time.monotonic() + 8
                    while True:
                        try:
                            async with session.get('http://127.0.0.1:{}'.format(port)) as response:
                                health = await response.json()
                                assert health['mock'] is False and health['mode'] == 'operations'
                                break
                        except aiohttp.ClientError:
                            assert time.monotonic() < deadline, 'Actual aiohttp server failed to start'
                            await asyncio.sleep(.05)
                    async with session.ws_connect('http://127.0.0.1:{}/ws?role=admin'.format(port)) as ws:
                        hello = await ws.receive_json()
                        assert hello['type'] == 'hello' and hello['mock'] is False

                        async def receive():
                            async for message in ws:
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    continue
                                state = json.loads(message.data)
                                if state.get('type') == 'state':
                                    states.append((time.monotonic(), state))
                                    run_state = state['run']['state']
                                    if not seen_states or seen_states[-1]['state'] != run_state:
                                        seen_states.append(dict(at=time.monotonic(), state=run_state))

                        reader = asyncio.create_task(receive())
                        await ws.send_json(dict(cmd='preflight'))
                        await until(lambda: states and states[-1][1]['run']['state'] == 'READY')
                        await ws.send_json(dict(cmd='start'))
                        await until(lambda: states and states[-1][1]['run']['state'] == 'RUNNING'
                                    and states[-1][1]['run']['bt_pid'] is not None
                                    and len([r for r in read_records(root / 'children.jsonl') if r['kind'] == 'started']) == 2)
                        assert all(states[-1][1]['robots'][role]['pose_age'] < .2 for role in cfg.drones)
                        assert states[-1][1]['mission']['phase'] == 'capture'
                        if name == 'reset-race':
                            await ws.send_json(dict(cmd='reset'))
                            await until(lambda: states[-1][1]['run']['state'] == 'LANDING')
                        assert not ws.closed, 'Admin WebSocket must remain open at server SIGINT'
                        requested = time.monotonic()
                        process.send_signal(signal.SIGINT)
                        try:
                            await asyncio.wait_for(process.wait(), EXIT_BOUND)
                        except asyncio.TimeoutError as exc:
                            raise AssertionError('SERVER SHUTDOWN STALL: still alive after {}s with open admin WS'.format(EXIT_BOUND)) from exc
                        elapsed = time.monotonic() - requested
                        await asyncio.wait_for(reader, 1)
                        assert ws.closed, 'Server did not close the admin WebSocket'
                        socket_code = ws.close_code
                records = read_records(root / 'server.jsonl')
                children = read_records(root / 'children.jsonl')
                final = next(item for item in records if item['kind'] == 'shutdown_complete')
                cleanup = next(item for item in records if item['kind'] == 'cleanup_complete')
                expected_state = 'IDLE' if name == 'reset-race' else 'ABORTED'
                assert final['state']['run']['state'] == expected_state, final
                assert final['ros_alive'] and final['open_sockets'] == 0
                assert cleanup['ros_closed'] and cleanup['tasks_done']
                assert final['at'] <= cleanup['at']
                assert all(child['returncode'] is not None and child['sigint_sent'] for child in final['children'].values())
                assert not list((root / 'dashboard/run').glob('*.pid'))
                for event in children:
                    if event['kind'] == 'started':
                        assert event['pid'] == event['sid'] and event['core_limit'] == [0, 0]
                        assert not Path('/proc/{}'.format(event['pid'])).exists(), event
                signals = [event for event in children if event['kind'] == 'signal']
                assert [(event['child'], event['signal']) for event in signals] == [('bt', signal.SIGINT), ('preflight', signal.SIGINT)]
                with ros.lock:
                    requests = list(ros.requests)
                lands = [event for event in requests if event['channel'] == 'land']
                cancels = [event for event in requests if event['channel'] == 'cancel']
                arms = [event for event in requests if event['channel'] == 'arm']
                assert sorted(event['robot'] for event in lands) == sorted(cfg.drones)
                assert sorted(event['robot'] for event in cancels) == sorted(cfg.limos)
                assert all(event['wire'] == dict(height=0.0, group_mask=0, sec=0, nanosec=250000000) for event in lands)
                assert all(event['wire'] == dict(uuid=[0] * 16, sec=0, nanosec=0) for event in cancels)
                assert min(event['at'] for event in lands) - signals[0]['at'] >= .48
                assert all(event['at'] < signals[1]['at'] for event in lands + cancels)
                assert all(event['wire'] == dict(arm=False) and event['at'] > signals[1]['at'] for event in arms)
                warnings = [event['text'] for event in final['state']['events'] if event['level'] == 'warning']
                if name == 'early-exit':
                    assert not arms, 'Airborne fresh pose must never be disarmed merely because BT exited'
                    assert all(any(role + ' 착륙 미확인' in warning for warning in warnings) for role in cfg.drones)
                    assert final['children']['bt']['returncode'] == 0
                else:
                    assert sorted(event['robot'] for event in arms) == sorted(cfg.drones), dict(
                        requests=requests, warnings=warnings,
                        poses={role: final['state']['robots'][role] for role in cfg.drones})
                    assert signals[1]['at'] - min(event['at'] for event in lands) >= LAND_DURATION + 1.9
                    assert final['children']['bt']['returncode'] == -signal.SIGKILL
                if name == 'reset-race':
                    assert final['state']['mission'] is None and final['state']['preflight'] is None
                    assert all(row['led'] == 'off' and row['cmd'] is None and row['detections'] == []
                               for row in final['state']['robots'].values() if row['kind'] == 'drone')
                assert process.returncode == 0, (process.returncode, (root / 'stdout.log').read_text())
                timeline = sorted(signals + requests + seen_states + [dict(kind='server_SIGINT', at=requested),
                    dict(kind='shutdown_complete', at=final['at'], state=expected_state),
                    dict(kind='cleanup_complete', at=cleanup['at'])], key=lambda item: item['at'])
                for event in timeline:
                    event['relative_s'] = round(event.pop('at') - requested, 4)
                print('SCENARIO', json.dumps(dict(name=name, exit_seconds=round(elapsed, 4),
                    websocket_close_code=socket_code, final_state=expected_state,
                    production_mock=False, readiness_fixture_only=True,
                    children=final['children'], timeline=timeline, warnings=warnings), ensure_ascii=False), flush=True)
        finally:
            if process is not None and process.returncode is None:
                # Leave the server alive long enough to reap its own test
                # children, including in the deliberately broken-hook case.
                kill_verified_children(root)
                pids = [row['pid'] for row in read_records(root / 'children.jsonl')
                        if row['kind'] == 'started']
                deadline = time.monotonic() + 2
                while any(Path('/proc/{}'.format(pid)).exists() for pid in pids) and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
                process.kill()
                await process.wait()
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            kill_verified_children(root)
            await asyncio.to_thread(ros.close)
            if (root / 'stdout.log').exists():
                print('SERVER_LOG', name, (root / 'stdout.log').read_text().strip(), flush=True)


async def probe(args):
    assert os.environ.get('ROS_DOMAIN_ID') == '90', 'Use isolated ROS_DOMAIN_ID=90'
    assert os.environ.get('ROS_LOCALHOST_ONLY') == '1', 'Use ROS_LOCALHOST_ONLY=1'
    names = [args.scenario] if args.scenario else ['stubborn', 'early-exit', 'reset-race']
    print('SCOPE: production aiohttp web.run_app, Dashboard, Runner and ROSIO; actual local DDS; '
          'safe child commands; readiness checklist fixture; open admin WS; no hardware', flush=True)
    for name in names:
        await scenario(name, args.regression_late_cleanup)
    print('PASS: selected scenarios {}; server SIGINT closes open admin WS within {}s; '
          'DDS wire requests, signal order, pose-based disarm decision, expected final state, '
          'ROS cleanup order and child/pidfile cleanup assertions passed'.format(', '.join(names), EXIT_BOUND), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('stubborn', 'early-exit', 'reset-race'))
    parser.add_argument('--regression-late-cleanup', action='store_true')
    parser.add_argument('--server-child', action='store_true')
    parser.add_argument('--child', choices=('bt', 'preflight'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--prefix')
    parser.add_argument('--port', type=int)
    parser.add_argument('--config')
    arguments = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if arguments.child:
        safe_child(arguments)
    elif arguments.server_child:
        server_child(arguments)
    else:
        asyncio.run(probe(arguments))
