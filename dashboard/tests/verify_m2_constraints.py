#!/usr/bin/env python3
"""Audit the M2 runtime source constraints without traversing vendored trees."""
import ast
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
# Internal schema keys are not external robot/topic/service/action names.
INTERNAL_KEYS = {'limo_status_template', 'limo_status', 'limo_ips', 'limo_battery_v',
                 'limo_at', 'spare_prefix', 'spare_unarmed', 'spare_area'}
LITERAL_PATTERNS = [r'cf23\w*', r'/preflight(?:/|$)', r'/coshow(?:/|$)',
                    r'/(?:land|arm)(?:/|$)', r'navigate_to_pose', r'/aideck(?:/|$)',
                    r'marker_detections', r'\blimo_[A-Za-z0-9_]*', r'\bspare_[A-Za-z0-9_]*']


def assert_external_names_absent(tree, path):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for pattern in LITERAL_PATTERNS:
            for match in re.finditer(pattern, node.value):
                assert match.group() in INTERNAL_KEYS, (path, node.lineno, pattern, match.group())



def main():
    print('Python:', sys.version.split()[0])
    imports = set()
    paths = sorted(ROOT.glob('*.py'))
    for path in paths:
        text = path.read_text()
        tree = ast.parse(text, feature_version=(3, 10))
        assert_external_names_absent(tree, path)
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
            # Vendor licenses/documentation contain URLs, so check executable
            # dependency/request syntax. Browser network QA verifies actual I/O.
            requests = r"(?:\bfrom\s*|\bimport\s*\(?|\bfetch\s*\(|\b(?:src|href)\s*=)\s*['\"](?:https?:)?//|url\s*\(\s*['\"]?(?:https?:)?//"
            assert not re.search(requests, path.read_text()), path
    assert not any('socket' == name for name in imports), 'Runtime must not open UDP sockets'
    print('Runtime imports:', ', '.join(sorted(imports)))
    print('PASS: {} runtime files, Python 3.10 grammar, no match/union/newer stdlib'.format(len(paths)))
    print('External literal patterns:', LITERAL_PATTERNS)
    print('Internal schema-key allowlist:', sorted(INTERNAL_KEYS))
    print('PASS: every runtime string literal tested; no external-name literal matched outside schema keys')
    print('PASS: only aiohttp/PyYAML + stdlib/ROS; no socket/UDP module')
    print('PASS: no external dependency/request syntax in static HTML/JS/CSS; vendor comment URLs are allowed')


if __name__ == '__main__':
    main()
