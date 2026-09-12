#!/usr/bin/env python3
"""Check tracked upstream paths and explicitly listed AST invariants.

This is a scope/grammar check, not a behavioral safety proof. It lists changed
functions as well as the definitions whose original ASTs were compared.
"""
import ast
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASE = '2a464d4007edfed09760f42c0391de6f38cb5f9a'
M1_BASE = '73c14ea'
ALLOWED = {
    'bt/scenarios/coshow/bt_nodes.py',
    'tools/preflight_node.py',
    'ros2_ws/src/aideck_aruco_ros/aideck_aruco_ros/aideck_aruco_node.py',
    'ros2_ws/src/aideck_aruco_ros/launch/aideck_aruco.launch.py',
}


def original(path, revision=BASE):
    return subprocess.check_output(['git', 'show', revision + ':' + path], cwd=str(ROOT), text=True)


def definitions(source):
    return {node.name: ast.dump(node) for node in ast.parse(source).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def functions(source):
    """Top-level functions and class methods; nested bodies remain in their owner."""
    result = {}

    def collect(body, prefix=''):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + node.name] = ast.dump(node)
            elif isinstance(node, ast.ClassDef):
                collect(node.body, prefix + node.name + '.')

    collect(ast.parse(source).body)
    return result


def check_service_telemetry_only(before_source, after_source):
    """Only the authorized numeric goal slice may change in _DroneService."""
    before = next(n for n in ast.parse(before_source).body
                  if isinstance(n, ast.ClassDef) and n.name == '_DroneService')
    after = next(n for n in ast.parse(after_source).body
                 if isinstance(n, ast.ClassDef) and n.name == '_DroneService')
    target = ast.dump(ast.parse("bb['cmd'][robot] = None").body[0].targets[0])
    changes = 0
    for node in ast.walk(before):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and ast.dump(node.targets[0]) == target):
            assert isinstance(node.value, ast.Dict)
            for index, key in enumerate(node.value.keys):
                if isinstance(key, ast.Constant) and key.value == 'goal':
                    assert ast.dump(node.value.values[index]) == ast.dump(
                        ast.parse('sig[1:]', mode='eval').body)
                    node.value.values[index] = ast.parse('sig[2:]', mode='eval').body
                    changes += 1
    assert changes == 1, 'expected one original cmd.goal assignment'
    assert ast.dump(before) == ast.dump(after), '_DroneService changed beyond cmd.goal[2:]'


def main():
    changed = subprocess.check_output(['git', 'diff', '--name-only', BASE], cwd=str(ROOT), text=True).splitlines()
    outside = [p for p in changed if not p.startswith('dashboard/')]
    assert set(outside) <= ALLOWED, outside
    bt = 'bt/scenarios/coshow/bt_nodes.py'
    before, after = definitions(original(bt)), definitions((ROOT / bt).read_text())
    preserved = [name for name in before if name not in ('UpdateBlackboard', '_DroneService')]
    for name in preserved:
        assert before[name] == after[name], 'BT AST changed: ' + name
    print('BT top-level function/class ASTs unchanged ({}): {}'.format(
        len(preserved), ', '.join(preserved)))
    old_functions, new_functions = functions(original(bt)), functions((ROOT / bt).read_text())
    callbacks = ['UpdateBlackboard.' + name for name in (
        '_on_drone_pose', '_on_preflight', '_on_limo_pose', '_on_det')]
    for name in callbacks:
        assert old_functions[name] == new_functions[name], 'BT callback AST changed: ' + name
    print('BT callback ASTs unchanged: ' + ', '.join(callbacks))
    check_service_telemetry_only(original(bt), (ROOT / bt).read_text())
    print('BT _DroneService AST: only authorized cmd.goal slice[1:] -> slice[2:] differs')
    path = 'tools/preflight_node.py'
    old, new = ast.parse(original(path)), ast.parse((ROOT / path).read_text())
    old_class = next(n for n in old.body if isinstance(n, ast.ClassDef) and n.name == 'Preflight')
    new_class = next(n for n in new.body if isinstance(n, ast.ClassDef) and n.name == 'Preflight')
    old_methods = {n.name: ast.dump(n) for n in old_class.body if isinstance(n, ast.FunctionDef)}
    new_methods = {n.name: ast.dump(n) for n in new_class.body if isinstance(n, ast.FunctionDef)}
    safety = ['_pose_ok', '_supervisor_ok', '_postarm_ok', 'check_inflight', 'land_all', 'run']
    for name in safety:
        assert old_methods[name] == new_methods[name], 'Preflight safety AST changed: ' + name
    assert definitions(original(path))['main'] == definitions((ROOT / path).read_text())['main']
    print('Preflight ASTs unchanged: ' + ', '.join(safety + ['main']))

    def log_calls(tree):
        return [ast.dump(n) for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Call)
                and isinstance(n.func.value.func, ast.Attribute)
                and n.func.value.func.attr == 'get_logger']

    # ast.walk is breadth-first: adding a lock changes traversal order, not logs.
    assert sorted(log_calls(old)) == sorted(log_calls(new))
    print('Preflight original logger calls unchanged:', len(log_calls(old)))
    for path in sorted(ALLOWED):
        current = functions((ROOT / path).read_text())
        for revision in (BASE, M1_BASE):
            previous = functions(original(path, revision))
            modified = sorted(name for name in previous if current.get(name) != previous[name])
            added = sorted(set(current) - set(previous))
            print('{} vs {}: modified={}, added={}'.format(path, revision[:7], modified, added))
    python_files = [ROOT / p for p in ALLOWED]
    python_files += [p for p in (ROOT / 'dashboard').rglob('*.py')
                     if 'run' not in p.relative_to(ROOT / 'dashboard').parts]
    for path in python_files:
        ast.parse(path.read_text(), feature_version=(3, 10))
    print('Python 3.10 grammar:', len(python_files), 'files PASS')
    print('Upstream changed paths:', sorted(outside))
    print('PASS: tracked upstream paths limited to allowlist; listed AST and Python grammar checks passed')


if __name__ == '__main__':
    main()
