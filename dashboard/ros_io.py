"""The sole ROS boundary: configured interfaces become plain dashboard values.

Importing this module does not import ROS, so mock mode also works without ROS.
ROS callbacks write directly to the thread-safe store on their executor thread,
so receipt timestamps and latest-frame slots do not depend on asyncio progress.
"""
import asyncio
import json
import math
import threading
import time
import uuid


# These are internal configuration keys and channels, never external ROS names.
_INTERFACES = {
    'topics': {
        'pose_template': ('drones', 'pose'),
        'status_template': ('all_drones', 'status'),
        'odom_template': ('all_limos', 'pose'),
        'limo_status_template': ('all_limos', 'limo_status'),
        'camera_template': ('drones', 'frame'),
        'camera_fps_template': ('drones', 'camera_fps'),
        'camera_ok_template': ('drones', 'camera_ok'),
        'detections_template': ('drones', 'detections'),
        'mission_state': ('global', 'mission'),
        'preflight_ready': ('global', 'ready'),
        'preflight_status': ('global', 'preflight'),
    },
    'services': {'land_template': ('drones', 'land'),
                 'arm_template': ('drones', 'arm')},
    'actions': {'nav_template': ('limos', 'nav_ready')},
}


def interface_specs(cfg):
    """Expand every configured interface; spare robots have no control clients."""
    groups = {
        'drones': cfg.drones, 'limos': cfg.limos, 'global': [None],
        'all_drones': [name for name, data in cfg.robots.items()
                       if data['kind'] == 'drone'],
        'all_limos': [name for name, data in cfg.robots.items()
                     if data['kind'] == 'limo'],
    }
    specs = []
    for kind, definitions in _INTERFACES.items():
        for key, template in cfg.raw.get(kind, {}).items():
            if key not in definitions:
                raise ValueError('Unsupported configured interface: {}.{}'.format(kind, key))
            group, channel = definitions[key]
            if not isinstance(template, str) or not template:
                raise ValueError('Missing interface name: {}.{}'.format(kind, key))
            for robot in groups[group]:
                try:
                    name = template.format(cf=robot, limo=robot, name=robot)
                except (KeyError, ValueError) as exc:
                    raise ValueError('Invalid {}.{}: {}'.format(kind, key, exc)) from exc
                specs.append({
                    'kind': kind, 'key': key, 'robot': robot, 'channel': channel,
                    'name': name, 'type': cfg.raw.get('types', {}).get(kind, {}).get(key),
                })
    return specs


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Non-finite telemetry')
    return value


def _protocol_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _protocol_object(value, path):
    if not isinstance(value, dict):
        raise ValueError('{} must be an object'.format(path))
    return value


def _protocol_fields(value, checks, path):
    _protocol_object(value, path)
    for key, valid in checks.items():
        if key in value and not valid(value[key]):
            raise ValueError('Invalid {}.{}'.format(path, key))


def _validate_protocol(channel, value):
    """Validate known protocol fields while preserving future optional keys."""
    text = lambda item: isinstance(item, str)
    flag = lambda item: type(item) is bool
    integer = lambda item: type(item) is int and item >= 0
    nonnegative = lambda item: _protocol_number(item) and item >= 0
    optional_text = lambda item: item is None or text(item)
    numbers = lambda items: isinstance(items, list) and all(_protocol_number(item) for item in items)
    vector = lambda item: item is None or (numbers(item) and len(item) == 3)
    _protocol_fields(value, {'t': nonnegative}, channel)
    if channel == 'mission':
        _protocol_fields(value, {
            'phase': lambda item: text(item) and bool(item),
            'mission_marker_id': lambda item: item is None or integer(item),
            'target_id': lambda item: item is None or integer(item),
            'finder': optional_text, 'target_confirm_note': optional_text,
            'target_confirmed': flag, 'preflight_required': flag, 'preflight_ready': flag,
            'rescue_done_t': nonnegative,
            'missing_pose': lambda items: isinstance(items, list) and all(text(item) for item in items),
        }, channel)
        for robot, color in _protocol_object(value.get('led', {}), 'mission.led').items():
            if not text(color):
                raise ValueError('Invalid mission.led.{}'.format(robot))
        for robot, command in _protocol_object(value.get('cmd', {}), 'mission.cmd').items():
            _protocol_fields(command, {'kind': text, 'goal': numbers, 't': nonnegative},
                             'mission.cmd.{}'.format(robot))
        for robot, index in _protocol_object(value.get('search_progress', {}),
                                             'mission.search_progress').items():
            if not integer(index):
                raise ValueError('Invalid mission.search_progress.{}'.format(robot))
        if value.get('P_N') is not None:
            _protocol_fields(value['P_N'], {key: _protocol_number for key in ('x', 'y', 'z')},
                             'mission.P_N')
    else:
        _protocol_fields(value, {'stage': integer, 'ready': flag, 'abort_reason': optional_text}, channel)
        stages = value.get('stages', [])
        if not isinstance(stages, list):
            raise ValueError('preflight.stages must be a list')
        for index, stage in enumerate(stages):
            _protocol_fields(stage, {'name': text, 'result': text},
                             'preflight.stages[{}]'.format(index))
        report_checks = {key: flag for key in ('kal_ok', 'pose_ok', 'sup_ok', 'armed', 'can_fly')}
        report_checks.update(sup_why=text, pose_err=vector, kal_range=vector,
                             battery_v=lambda item: item is None or nonnegative(item))
        for robot, report in _protocol_object(value.get('drones', {}), 'preflight.drones').items():
            _protocol_fields(report, report_checks, 'preflight.drones.{}'.format(robot))


def decode(channel, message):
    """All external message field/bit interpretation lives in this function."""
    if channel == 'pose':
        pose = message.pose
        if hasattr(pose, 'pose'):
            pose = pose.pose
        position, orientation = pose.position, pose.orientation
        qx, qy, qz, qw = [_finite(getattr(orientation, key)) for key in ('x', 'y', 'z', 'w')]
        return {'x': _finite(position.x), 'y': _finite(position.y),
                'z': _finite(position.z),
                'yaw': math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))}
    if channel == 'status':
        bits = int(message.supervisor_info)
        return {
            'battery_v': _finite(message.battery_voltage), 'rssi': int(message.rssi),
            'armed': bool(bits & message.SUPERVISOR_INFO_IS_ARMED),
            'can_fly': bool(bits & message.SUPERVISOR_INFO_CAN_FLY),
            'tumbled': bool(bits & message.SUPERVISOR_INFO_IS_TUMBLED),
            'low_power': int(message.pm_state) == message.PM_STATE_LOW_POWER,
        }
    if channel == 'limo_status':
        return {'battery_v': _finite(message.battery_voltage)}
    if channel == 'frame':
        return bytes(message.data)
    if channel == 'detections':
        return [int(marker.id) for marker in message.markers]
    if channel == 'camera_fps':
        return _finite(message.data)
    if channel in ('camera_ok', 'ready'):
        if not isinstance(message.data, bool):
            raise ValueError('Expected a boolean')
        return message.data
    if channel in ('mission', 'preflight'):
        value = json.loads(message.data)
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object')
        # Reject non-standard NaN/Infinity before values reach WebSocket JSON.
        json.dumps(value, allow_nan=False)
        _validate_protocol(channel, value)
        return value
    raise ValueError('Unsupported channel: {}'.format(channel))


def _load_type(spec):
    from rosidl_runtime_py.utilities import get_action, get_message, get_service

    if not isinstance(spec['type'], str) or not spec['type']:
        raise ValueError('No configured type for {}'.format(spec['key']))
    return {'topics': get_message, 'services': get_service,
            'actions': get_action}[spec['kind']](spec['type'])


def _new_node(prefix):
    import rclpy
    from rclpy.context import Context
    from rclpy.signals import SignalHandlerOptions

    context = Context()
    rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
    try:
        node = rclpy.create_node(prefix + uuid.uuid4().hex[:10], context=context,
                                 use_global_arguments=False)
    except Exception:
        context.shutdown()
        raise
    return context, node


def _qos(channel):
    from rclpy.qos import (DurabilityPolicy, QoSProfile, ReliabilityPolicy,
                          qos_profile_sensor_data)

    if channel in ('mission', 'preflight', 'ready'):
        return QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.TRANSIENT_LOCAL)
    if channel == 'detections':
        return QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.VOLATILE)
    return qos_profile_sensor_data


class ROSIO:
    """Own the observation node and role-only, currently uncalled clients."""

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.specs = interface_specs(cfg)
        self.node = self.context = self.executor = self.thread = None
        self.service_clients, self.action_clients = {}, {}
        self.subscriptions = []
        self._stopped = threading.Event()

    async def start(self):
        from rclpy.action import ActionClient
        from rclpy.executors import SingleThreadedExecutor

        if self.node is not None:
            return
        self._stopped.clear()
        self.context, self.node = _new_node('dashboard_observer_')
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        try:
            for spec in self.specs:
                try:
                    typename = _load_type(spec)
                    if spec['kind'] == 'topics':
                        self.subscriptions.append(self.node.create_subscription(
                            typename, spec['name'], self._callback(spec), _qos(spec['channel'])))
                    elif spec['kind'] == 'services':
                        self.service_clients[(spec['robot'], spec['channel'])] = (
                            self.node.create_client(typename, spec['name']))
                    else:
                        self.action_clients[spec['robot']] = ActionClient(
                            self.node, typename, spec['name'])
                except Exception as exc:
                    self._unavailable(spec['robot'], spec['channel'], exc)
            self.node.create_timer(0.5, self._poll_graph)
            self.thread = threading.Thread(target=self._spin, name='dashboard-ros', daemon=True)
            self.thread.start()
        except Exception:
            await self.close()
            raise

    def _dispatch(self, method, *args):
        if not self._stopped.is_set():
            getattr(self.store, method)(*args)

    def _unavailable(self, robot, channel, exc):
        self._dispatch('unavailable', robot, channel, '정보 없음: {}'.format(exc))

    def _callback(self, spec):
        def receive(message):
            try:
                value = decode(spec['channel'], message)
                if spec['robot'] is None:
                    self._dispatch(spec['channel'], value)
                else:
                    self._dispatch('receive', spec['robot'], spec['channel'], value)
            except Exception as exc:
                self._unavailable(spec['robot'], spec['channel'], exc)
        return receive

    def _poll_graph(self):
        try:
            # Keep duplicates: the external-process check counts equal node names.
            self._dispatch('nodes', self.node.get_node_names_and_namespaces())
        except Exception as exc:
            self._unavailable(None, 'nodes', exc)
        for robot, client in self.action_clients.items():
            try:
                self._dispatch('receive', robot, 'nav_ready', client.server_is_ready())
            except Exception as exc:
                self._unavailable(robot, 'nav_ready', exc)

    def _spin(self):
        while not self._stopped.is_set():
            try:
                self.executor.spin_once(timeout_sec=0.1)
            except Exception as exc:
                if not self._stopped.is_set():
                    self._unavailable(None, 'ros', exc)
                    self._stopped.wait(0.1)

    async def close(self):
        self._stopped.set()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join, 2)
        if self.executor is not None:
            self.executor.shutdown(timeout_sec=1)
        for client in self.action_clients.values():
            client.destroy()
        if self.node is not None:
            self.node.destroy_node()
        if self.context is not None and self.context.ok():
            self.context.shutdown()
        self.node = self.context = self.executor = None
        self.action_clients.clear()
        self.service_clients.clear()
        self.subscriptions.clear()


def _server_graph(node):
    """Only publishing/server endpoints count; observing clients cannot pass."""
    from rclpy.action.graph import get_action_server_names_and_types_by_node

    graph = {'topics': {}, 'services': {}, 'actions': {}}
    nodes = node.get_node_names_and_namespaces()
    for name, _types in node.get_topic_names_and_types():
        graph['topics'][name] = {info.topic_type for info in node.get_publishers_info_by_topic(name)}
    for name, namespace in set(nodes):
        for kind, read in (
                ('services', lambda: node.get_service_names_and_types_by_node(name, namespace)),
                ('actions', lambda: get_action_server_names_and_types_by_node(node, name, namespace))):
            try:
                for endpoint, types in read():
                    graph[kind].setdefault(endpoint, set()).update(types)
            except Exception:
                # A disappearing remote node must not hide the remaining graph.
                continue
    return graph, nodes


def check_config(cfg, timeout=3):
    """Inspect from a separate, unsubscribed node and return all PASS/FAIL rows.

    This synchronous CLI helper must run before ROSIO.start(). No subscriber,
    service client, or action client is created by the checker itself.
    """
    try:
        specs = interface_specs(cfg)
    except (ValueError, KeyError, TypeError) as exc:
        return [{'kind': 'config', 'key': 'interfaces', 'name': '', 'expected_type': None,
                 'actual_types': [], 'ok': False, 'detail': str(exc)}]
    rows = []
    for spec in specs:
        error = None
        try:
            _load_type(spec)
        except Exception as exc:
            error = '정보 없음: {}'.format(exc)
        rows.append({'kind': spec['kind'], 'key': spec['key'], 'name': spec['name'],
                     'expected_type': spec['type'], 'actual_types': [],
                     'ok': False, 'detail': error or '발행자/서버 없음'})
    type_errors = [row['detail'] if row['detail'].startswith('정보 없음:') else None for row in rows]
    for key, name in cfg.raw.get('nodes', {}).items():
        rows.append({'kind': 'nodes', 'key': key, 'name': name, 'expected_type': None,
                     'actual_types': [], 'ok': False, 'detail': '노드 없음'})
    try:
        context, node = _new_node('dashboard_config_check_')
    except Exception as exc:
        for row in rows:
            row['detail'] = 'ROS 그래프 정보 없음: {}'.format(exc)
        return rows
    try:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            graph, nodes = _server_graph(node)
            for index, spec in enumerate(specs):
                row = rows[index]
                # Node name expansion handles absolute and relative configuration.
                resolved = node.resolve_topic_name(spec['name']) if spec['kind'] == 'topics' else (
                    node.resolve_service_name(spec['name']))
                actual = sorted(graph[spec['kind']].get(resolved, set()))
                row['actual_types'] = actual
                row['ok'] = type_errors[index] is None and spec['type'] in actual
                row['detail'] = type_errors[index] or (
                    'PASS' if row['ok'] else '타입 불일치' if actual else '발행자/서버 없음')
            full_names = {namespace.rstrip('/') + '/' + name for name, namespace in nodes}
            for row in rows[len(specs):]:
                row['ok'] = '/' + str(row['name']).strip('/') in full_names
                row['detail'] = 'PASS' if row['ok'] else '노드 없음'
            if all(row['ok'] for row in rows) or time.monotonic() >= deadline:
                return rows
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except Exception as exc:
        for row in rows:
            row['ok'] = False
            row['detail'] = 'ROS 그래프 정보 없음: {}'.format(exc)
        return rows
    finally:
        node.destroy_node()
        context.shutdown()
