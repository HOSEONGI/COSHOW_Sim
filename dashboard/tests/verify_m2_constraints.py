#!/usr/bin/env python3
"""Audit the M2 runtime source constraints without traversing vendored trees."""
import ast
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    print('Python:', sys.version.split()[0])
    imports = set()
    paths = sorted(ROOT.glob('*.py'))
    for path in paths:
        text = path.read_text()
        tree = ast.parse(text, feature_version=(3, 10))
        assert not re.search(r'cf23|/preflight/|/coshow/', text), path
        assert not re.search(r'use_sim_time|tomllib|TaskGroup|asyncio\.timeout', text), path
        assert not any(isinstance(n, ast.Match) for n in ast.walk(tree)), path
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split('.')[0])
            annotation = getattr(node, 'annotation', None)
            if annotation is not None:
                assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                               for n in ast.walk(annotation)), path
    extras = imports - set(sys.stdlib_module_names) - {
        'dashboard', 'aiohttp', 'yaml', 'rclpy', 'rosidl_runtime_py'}
    assert not extras, extras
    static = list((ROOT / 'static').rglob('*'))
    for path in static:
        if path.suffix in ('.html', '.js', '.css'):
            assert not re.search(r'https?://|@import', path.read_text()), path
    assert not any('socket' == name for name in imports), 'Runtime must not open UDP sockets'
    print('Runtime imports:', ', '.join(sorted(imports)))
    print('PASS: {} runtime files, Python 3.10 grammar, no match/union/newer stdlib'.format(len(paths)))
    print('PASS: grep cf23|/preflight/|/coshow/ dashboard/*.py has no matches')
    print('PASS: only aiohttp/PyYAML + stdlib/ROS; no socket/UDP module')
    print('PASS: no external URL/import in static HTML/JS/CSS (M2 has only local JPEG)')


if __name__ == '__main__':
    main()
