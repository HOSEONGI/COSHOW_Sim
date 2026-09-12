#!/usr/bin/env python3
"""Verify upstream scope and unchanged safety definitions against the handoff."""
import ast
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASE = '2a464d4007edfed09760f42c0391de6f38cb5f9a'
ALLOWED = {
    'bt/scenarios/coshow/bt_nodes.py',
    'tools/preflight_node.py',
    'ros2_ws/src/aideck_aruco_ros/aideck_aruco_ros/aideck_aruco_node.py',
    'ros2_ws/src/aideck_aruco_ros/launch/aideck_aruco.launch.py',
}


def original(path):
    return subprocess.check_output(['git', 'show', BASE + ':' + path], cwd=str(ROOT), text=True)


def definitions(source):
    return {node.name: ast.dump(node) for node in ast.parse(source).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def main():
    changed = subprocess.check_output(['git', 'diff', '--name-only', BASE], cwd=str(ROOT), text=True).splitlines()
    outside = [p for p in changed if not p.startswith('dashboard/')]
    assert set(outside) <= ALLOWED, outside
    bt = 'bt/scenarios/coshow/bt_nodes.py'
    before, after = definitions(original(bt)), definitions((ROOT / bt).read_text())
    preserved = [name for name in before if name != 'UpdateBlackboard']
    assert all(before[name] == after[name] for name in preserved)
    print('BT original control/safety definitions unchanged:', len(preserved))
    path = 'tools/preflight_node.py'
    old, new = ast.parse(original(path)), ast.parse((ROOT / path).read_text())
    old_class = next(n for n in old.body if isinstance(n, ast.ClassDef) and n.name == 'Preflight')
    new_class = next(n for n in new.body if isinstance(n, ast.ClassDef) and n.name == 'Preflight')
    old_methods = {n.name: ast.dump(n) for n in old_class.body if isinstance(n, ast.FunctionDef)}
    new_methods = {n.name: ast.dump(n) for n in new_class.body if isinstance(n, ast.FunctionDef)}
    safety = ['_pose_ok', '_supervisor_ok', '_postarm_ok', 'check_inflight', 'land_all', 'run']
    assert all(old_methods[name] == new_methods[name] for name in safety)
    assert definitions(original(path))['main'] == definitions((ROOT / path).read_text())['main']
    print('Preflight safety methods + main unchanged:', len(safety) + 1)

    def log_calls(tree):
        return [ast.dump(n) for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Call)
                and isinstance(n.func.value.func, ast.Attribute)
                and n.func.value.func.attr == 'get_logger']

    # ast.walk is breadth-first: adding a lock changes traversal order, not logs.
    assert sorted(log_calls(old)) == sorted(log_calls(new))
    print('Preflight original logger calls unchanged:', len(log_calls(old)))
    python_files = [ROOT / p for p in ALLOWED]
    python_files += [p for p in (ROOT / 'dashboard').rglob('*.py')
                     if 'run' not in p.relative_to(ROOT / 'dashboard').parts]
    for path in python_files:
        ast.parse(path.read_text(), feature_version=(3, 10))
    print('Python 3.10 grammar:', len(python_files), 'files PASS')
    print('Upstream changed paths:', sorted(outside))
    print('PASS: protected files and existing control/safety definitions preserved')


if __name__ == '__main__':
    main()
