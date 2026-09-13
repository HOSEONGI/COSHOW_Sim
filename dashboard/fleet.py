"""Persistent role assignments, generated configuration and owned stack children.

Only generated files are written. Operator templates and BT coordinates remain
their authoritative inputs. This module never opens radios or camera sockets.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import inspect
import math
from pathlib import Path
import re

import yaml

from dashboard.config import HERE, load_config, read_yaml
from dashboard.runner import command_argv


@dataclass
class GeneratedConfig:
    files: dict
    roster_hash: str
    preflight_argv: list
    radio_counts: dict


def _inventory(cfg):
    rows = cfg.raw.get('fleet', {}).get('drones', [])
    if not isinstance(rows, list) or not rows:
        raise ValueError('플릿 드론 인벤토리가 없습니다')
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError('플릿 기체 id 형식 오류')
        if row['id'] in by_id:
            raise ValueError('플릿 기체 id 중복: ' + row['id'])
        by_id[row['id']] = row
    return by_id


def _radio(uri, label):
    if not isinstance(uri, str):
        raise ValueError(label + ': radio URI 미설정 또는 형식 오류')
    match = re.fullmatch(r'radio://(\d+)/(\d+)/(250K|1M|2M)/([0-9A-Fa-f]{10})', uri or '')
    if not match or not 0 <= int(match.group(2)) <= 125:
        raise ValueError(label + ': radio URI 미설정 또는 형식 오류')
    return match.group(1)


def _ip(row):
    # A role's old network fallback may belong to the replaced physical camera.
    value = row.get('aideck_ip')
    if not isinstance(value, str) or not value or any(char in value for char in ',\r\n'):
        raise ValueError(row['id'] + ': 역할 카메라 IP 미설정 또는 형식 오류')
    return value


def validate_roster(cfg, roster):
    by_id = _inventory(cfg)
    if not isinstance(roster, dict) or set(roster) != set(cfg.drones):
        raise ValueError('로스터는 모든 드론 역할을 정확히 한 번 배정해야 합니다')
    if any(not isinstance(value, str) for value in roster.values()):
        raise ValueError('로스터 물리 기체 id 형식 오류')
    if len(set(roster.values())) != len(roster):
        raise ValueError('로스터에 동일 물리 기체를 중복 배정할 수 없습니다')
    for role, physical in roster.items():
        if physical not in by_id:
            raise ValueError('인벤토리에 없는 물리 기체: ' + physical)
        if not isinstance(role, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', role):
            raise ValueError('역할 이름 형식 오류: ' + str(role))
        _radio(by_id[physical].get('uri'), physical)
        _ip(by_id[physical])
    return by_id


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _bases(cfg):
    bases = []
    for role in cfg.drones:
        base = cfg.bt.get('coshow', {}).get('drones', {}).get(role, {}).get('base')
        if not isinstance(base, (list, tuple)) or len(base) != 2 or not all(_number(n) for n in base):
            raise ValueError(role + ': BT base 설정 없음 또는 형식 오류')
        bases.append([float(base[0]), float(base[1])])
    return bases


def _grid(area, count):
    if not count:
        return []
    pitch = area.get('pitch')
    if not _number(pitch) or pitch <= 0:
        raise ValueError('스페어 격자 pitch 설정 오류')
    ranges = []
    for axis in ('x', 'y'):
        bounds = area.get(axis)
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not all(_number(n) for n in bounds) or bounds[0] > bounds[1]):
            raise ValueError('스페어 격자 범위 설정 오류')
        ranges.append([round(bounds[0] + i * pitch, 9)
                       for i in range(int(math.floor((bounds[1] - bounds[0]) / pitch + 1e-9)) + 1)])
    points = [[x, y, 0.0] for y in ranges[1] for x in ranges[0]]
    if len(points) < count:
        raise ValueError('스페어 격자 공간이 부족합니다')
    return points[:count]


def generate_config(cfg, roster=None, run_dir=None):
    roster = cfg.roster if roster is None else roster
    by_id = validate_roster(cfg, roster)
    bases = _bases(cfg)
    template_name = cfg.raw.get('crazyflies_template')
    if not template_name:
        raise ValueError('crazyflies_template 설정 없음')
    template = read_yaml(cfg.resolve(template_name))
    if not isinstance(template.get('robot_types'), dict) or 'cf21' not in template['robot_types']:
        raise ValueError('crazyflies_template의 cf21 기체 형식 설정 없음')
    camera_template = cfg.raw.get('aideck_template')
    camera_path = (cfg.resolve(camera_template) if camera_template else HERE.parent /
                   'ros2_ws/src' / 'aideck_aruco_ros' / 'config/drones.yaml')
    camera = read_yaml(camera_path)
    camera_keys = [key for key, value in camera.items()
                   if isinstance(value, dict) and isinstance(value.get('ros__parameters'), dict)
                   and 'drones' in value['ros__parameters']]
    if len(camera_keys) != 1:
        raise ValueError('카메라 템플릿에서 drones 파라미터 블록을 하나만 찾을 수 있어야 합니다')
    maximum = cfg.raw.get('fleet', {}).get('max_per_radio')
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise ValueError('fleet.max_per_radio 설정 오류')
    counts, uris = {}, set()
    for physical, row in by_id.items():
        uri = row.get('uri')
        radio = _radio(uri, physical)
        if uri.lower() in uris:
            raise ValueError('물리 기체 radio URI 중복: ' + uri)
        uris.add(uri.lower())
        counts[radio] = counts.get(radio, 0) + 1
        if counts[radio] > maximum:
            raise ValueError('라디오당 기체 수 초과: ' + radio)
    robots = {}
    for role, base in zip(cfg.drones, bases):
        robots[role] = dict(enabled=True, uri=by_id[roster[role]]['uri'],
                            initial_position=base + [0.0], type='cf21')
    unassigned = [physical for physical in by_id if physical not in roster.values()]
    prefix = cfg.raw.get('spare_prefix')
    if not isinstance(prefix, str) or not prefix:
        raise ValueError('스페어 이름 접두사 설정 없음')
    for physical, position in zip(unassigned, _grid(cfg.field.get('spare_area', {}), len(unassigned))):
        name = prefix + physical
        if name in robots:
            raise ValueError('스페어 이름과 역할 이름 중복: ' + name)
        robots[name] = dict(enabled=True, uri=by_id[physical]['uri'], initial_position=position, type='cf21')
    template['robots'] = robots
    camera[camera_keys[0]]['ros__parameters']['drones'] = [
        '{},{},{}'.format(role, _ip(by_id[roster[role]]), 5001 + index)
        for index, role in enumerate(cfg.drones)]
    values = dict(roles='[' + ','.join(cfg.drones) + ']',
                  expected_x='[' + ','.join(str(base[0]) for base in bases) + ']',
                  expected_y='[' + ','.join(str(base[1]) for base in bases) + ']')
    argv = command_argv(cfg, 'preflight', values)
    data = {'crazyflies.generated.yaml': template, 'drones.generated.yaml': camera,
            'preflight.generated.yaml': {'argv': argv}}
    files = {name: yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode('utf-8')
             for name, value in data.items()}
    digest = hashlib.sha256(b''.join(files[name] for name in sorted(files))).hexdigest()
    return GeneratedConfig(files, digest, argv, counts)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FleetManager:
    def __init__(self, cfg, store, on_reconfigure=None, run_dir=None):
        self.cfg, self.store, self.on_reconfigure = cfg, store, on_reconfigure
        self._run_dir_override = run_dir
        self.run_dir = self.roster_path = self.log_dir = None
        self._restore_mock_roster = cfg.mock
        self.children = {}
        self.lock = asyncio.Lock()
        self.generated = None
        self._generation_error = None
        self._closing = False
        self._retired = {kind: [] for kind in ('crazyflie_server', 'aideck')}
        self._recorded_exits = set()
        self.store.context.setdefault('stack_processes', {})
        if self.store.run['state'] == 'IDLE':
            self.regenerate()

    def _configure_paths(self):
        if not self.cfg.dashboard_ok:
            raise ValueError('대시보드 설정을 읽을 수 없어 플릿 파일을 생성할 수 없습니다')
        if self.cfg.mock:
            run_dir = Path(self._run_dir_override or HERE / 'run/mock').resolve()
            self.roster_path = run_dir / 'roster.yaml'
        else:
            value = self.cfg.raw.get('roster_file', 'run/roster.yaml')
            if not isinstance(value, str) or not value.strip():
                raise ValueError('roster_file: 비어 있지 않은 파일 경로가 필요합니다')
            self.roster_path = self.cfg.resolve(value)
            run_dir = Path(self._run_dir_override or self.roster_path.parent).resolve()
        if self.run_dir != run_dir:
            self.log_dir = run_dir.parent / 'logs'
        self.run_dir = run_dir

    def _idle(self):
        if self.store.run['state'] != 'IDLE':
            raise ValueError('플릿 변경과 스택 기동은 IDLE 상태에서만 허용됩니다')

    @asynccontextmanager
    async def _operation(self):
        async with self.lock:
            self._idle()
            if self._closing:
                raise ValueError('서버 종료 중입니다')
            self.store.set_stack(busy=True)
            try:
                yield
            finally:
                self.store.set_stack(busy=False)

    def regenerate(self):
        self._idle()
        if self._generation_error in self.cfg.errors:
            self.cfg.errors.remove(self._generation_error)
        try:
            self._configure_paths()
            if not self.roster_path.exists():
                _atomic_write(self.roster_path, yaml.safe_dump(self.cfg.roster, sort_keys=False).encode('utf-8'))
            elif self._restore_mock_roster:
                roster = read_yaml(self.roster_path)
                validate_roster(self.cfg, roster)
                self._apply_config(load_config(self.cfg.path, self.cfg.field_path, mock=True, roster_override=roster))
            self._restore_mock_roster = False
            generated = generate_config(self.cfg, run_dir=self.run_dir)
            for name, content in generated.files.items():
                _atomic_write(self.run_dir / name, content)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, yaml.YAMLError) as exc:
            self.generated = None
            self._generation_error = '플릿 생성: ' + str(exc)
            self.cfg.errors.append(self._generation_error)
            self.store.set_stack(roster_hash=None, generation_error=str(exc))
            self.store.event('warning', self._generation_error)
            return None
        self.generated = generated
        self._generation_error = None
        self.store.set_stack(roster_hash=generated.roster_hash, generation_error=None,
                             radios=len(generated.radio_counts), radio_counts=generated.radio_counts)
        return generated

    async def save_roster(self, roster, expected_hash=None):
        async with self._operation():
            if expected_hash is not None and (self.generated is None or expected_hash != self.generated.roster_hash):
                raise ValueError('로스터가 다른 관리자 화면에서 변경되었습니다. 최신 배정을 다시 검토하세요')
            validate_roster(self.cfg, roster)
            self._configure_paths()
            _atomic_write(self.roster_path, yaml.safe_dump(dict(roster), sort_keys=False).encode('utf-8'))
            self._restore_mock_roster = False
            fresh = load_config(self.cfg.path, self.cfg.field_path, mock=self.cfg.mock,
                                roster_override=roster)
            self._apply_config(fresh)
            self.regenerate()
            if self.on_reconfigure is not None:
                response = self.on_reconfigure(self.cfg)
                if inspect.isawaitable(response):
                    await response
            self.store.event('info', '로스터 저장 완료 · 스택 재기동 필요')
            return dict(self.cfg.roster)

    def _apply_config(self, fresh):
        with self.store.lock:
            self.cfg.__dict__.clear()
            self.cfg.__dict__.update(fresh.__dict__)
            self.store.cfg = self.cfg
            self.store.data = {name: {} for name in self.cfg.robots}
            self.store.detected = {name: {} for name in self.cfg.robots}
            self.store.frames.clear()
            self.store.reset_cached()

    def recommend_roster(self):
        snapshot = self.store.snapshot()
        freshness = self.cfg.raw.get('freshness_s', {}).get('status', 1.0)
        candidates = []
        inventory = _inventory(self.cfg)
        for name, robot in snapshot['robots'].items():
            physical = robot.get('fleet_id')
            if physical not in inventory or robot.get('kind') != 'drone':
                continue
            age, voltage = robot.get('status_age'), robot.get('battery_v')
            if not _number(age) or age >= freshness or not _number(voltage):
                continue
            try:
                _radio(inventory[physical].get('uri'), physical)
                _ip(inventory[physical])
            except ValueError:
                continue
            candidates.append((-voltage, physical))
        candidates.sort()
        if len(candidates) < len(self.cfg.drones):
            raise ValueError('링크와 배터리가 확인된 추천 기체가 부족합니다')
        return {role: physical for role, (_, physical) in zip(self.cfg.drones, candidates)}

    def _external(self, kind):
        key = 'server' if kind == 'crazyflie_server' else kind
        target = '/' + self.cfg.raw.get('nodes', {}).get(key, '').strip('/')
        count = sum(('/' + namespace.strip('/') + '/' + name).replace('//', '/') == target
                    for name, namespace in self.store.context.get('nodes', []))
        child = self.children.get(kind)
        alive = child is not None and child.returncode is None
        grace = self.cfg.raw.get('external_node_grace_s', 20.0)
        self._retired[kind] = [at for at in self._retired[kind] if self.store.clock() - at < grace]
        return count > int(alive) + len(self._retired[kind])

    def refresh(self):
        with self.store.lock:
            for kind in ('crazyflie_server', 'aideck'):
                child = self.children.get(kind)
                alive = child is not None and child.returncode is None
                if child is not None and not alive and child.pid not in self._recorded_exits:
                    self._recorded_exits.add(child.pid)
                    self._retired[kind].append(self.store.clock())
                    self.store.stack['applied_hash'] = None
                    self.store.event('info' if child.sigint_sent else 'error',
                                     '{} 프로세스 종료 rc={}'.format(kind, child.returncode))
                external = self._external(kind)
                self.store.context['stack_processes'][kind] = dict(
                    alive=alive, pid=child.pid if alive else None,
                    expected_nodes=int(alive) + len(self._retired[kind]),
                    exited_at=self._retired[kind][-1] if self._retired[kind] else None)
                self.store.stack[kind] = 'external' if external else ('up' if alive else 'down')

    def _commands(self, generated):
        values = dict(crazyflies_yaml=str(self.run_dir / 'crazyflies.generated.yaml'),
                      drones_yaml=str(self.run_dir / 'drones.generated.yaml'))
        return {kind: command_argv(self.cfg, kind, values)
                for kind in ('crazyflie_server', 'aideck')}

    async def _start(self):
        if self.cfg.mock:
            raise ValueError('MOCK 모드에서는 실제 스택 프로세스를 기동하지 않습니다')
        from dashboard.runner import spawn_process, stop_process
        self.refresh()
        if any(self.store.stack.get(kind) in ('up', 'external') for kind in ('crazyflie_server', 'aideck')):
            raise ValueError('이미 실행 중이거나 외부에서 기동한 스택이 있습니다')
        generated = self.regenerate()
        if generated is None:
            raise ValueError(self.store.stack['generation_error'])
        commands = self._commands(generated)
        started = []
        try:
            for kind, argv in commands.items():
                child = await spawn_process(argv, self.cfg.bt_cwd.parent,
                                            self.cfg.raw.get('commands', {}).get('env', {}),
                                            self.log_dir / (kind + '.log'), self.run_dir / (kind + '.pid'))
                self.children[kind] = child
                started.append(child)
            await asyncio.sleep(.05)
            if any(child.returncode is not None for child in started):
                raise ValueError('스택 프로세스가 기동 직후 종료되었습니다')
        except BaseException:
            for child in reversed(started):
                await stop_process(child, timeout_s=10, process_group=True)
            self.refresh()
            raise
        self.store.set_stack(applied_hash=generated.roster_hash)
        self.refresh()
        self.store.event('info', '스택 기동 완료')
        return dict(self.store.stack)

    async def start_stack(self):
        async with self._operation():
            return await self._start()

    async def restart_stack(self):
        async with self._operation():
            if self.cfg.mock:
                raise ValueError('MOCK 모드에서는 실제 스택 프로세스를 재기동하지 않습니다')
            from dashboard.runner import stop_process
            self.refresh()
            if any(self.store.stack.get(kind) == 'external' for kind in ('crazyflie_server', 'aideck')):
                raise ValueError('외부에서 기동한 스택은 소유 터미널에서 종료하세요')
            # Validate every generated byte and command before stopping a live stack.
            generated = generate_config(self.cfg, run_dir=self.run_dir)
            self._commands(generated)
            for child in reversed(list(self.children.values())):
                await stop_process(child, timeout_s=10, process_group=True)
            self.refresh()
            self.children.clear()
            self.store.set_stack(applied_hash=None)
            self.refresh()
            return await self._start()

    async def close(self):
        """Call only after the run controller has completed its shutdown sequence."""
        if self._closing:
            return
        self._closing = True
        from dashboard.runner import stop_process
        async with self.lock:
            for child in reversed(list(self.children.values())):
                await stop_process(child, timeout_s=10, process_group=True)
            self.refresh()
            self.children.clear()
            self.refresh()
