#!/usr/bin/env python3
"""Offline dashboard HTTP/WS core. Real process controls are added in M5."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web, WSMsgType
from dashboard.config import load_config
from dashboard.state import TelemetryStore
from dashboard.checklist import evaluate, external_observation


class SocketSlots:
    """A single independent sender consumes replaceable, bounded latest slots."""
    def __init__(self, ws, drones, timeout=5.0, on_error=None):
        self.ws, self.count, self.timeout = ws, drones, timeout
        self.state, self.frames = None, {}
        self.on_error = on_error
        self.wake = asyncio.Event()

    def offer(self, state, frames):
        self.state = state
        self.frames.update({i: data for i, data in frames.items() if 0 <= i < self.count})
        self.wake.set()

    async def run(self):
        try:
            while not self.ws.closed:
                await self.wake.wait()
                self.wake.clear()
                state, frames = self.state, self.frames
                self.state, self.frames = None, {}
                if state is not None:
                    await asyncio.wait_for(self.ws.send_str(state), self.timeout)
                for payload in frames.values():
                    await asyncio.wait_for(self.ws.send_bytes(payload), self.timeout)
        except Exception as exc:
            try:
                if self.on_error:
                    self.on_error('WebSocket 송신 종료: {}: {}'.format(type(exc).__name__, exc))
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.ws.close(), self.timeout)


class Dashboard:
    def __init__(self, cfg, mock=False, mock_fail=False):
        self.cfg, self.mock = cfg, mock
        self.store = TelemetryStore(cfg)
        self.sockets, self.tasks = set(), []
        self.io, self.world = None, None
        if mock:
            from dashboard.mock import MockWorld
            self.world = MockWorld(cfg, self.store, mock_fail)

    def state(self):
        snapshot = self.store.snapshot()
        ctx = self.store.checklist_context()
        now = self.store.clock()
        external = external_observation('bt', self.cfg, ctx, now)
        snapshot['run']['external_bt'] = external['active']
        snapshot['checklist'] = evaluate(snapshot, self.cfg, ctx, now)
        return snapshot

    async def aggregate(self):
        next_tick, last_frames, frame_at = time.monotonic(), {}, {}
        warned = False
        while True:
            try:
                if self.world:
                    self.world.tick()
                state = json.dumps(self.state(), ensure_ascii=False, allow_nan=False)
                frames, now = {}, time.monotonic()
                period = 1 / max(.1, float(self.cfg.raw.get('frame_forward_max_fps', 15)))
                for name, (serial, jpeg) in self.store.latest_frames().items():
                    if name not in self.cfg.drones:
                        continue
                    if serial != last_frames.get(name) and now - frame_at.get(name, -float('inf')) >= period:
                        frames[self.cfg.drones.index(name)] = bytes([self.cfg.drones.index(name)]) + jpeg
                        last_frames[name], frame_at[name] = serial, now
                for slots in tuple(self.sockets):
                    slots.offer(state, frames)
            except Exception as exc:
                if not warned:
                    self.store.event('warning', '상태 집계 오류 (다음 tick 재시도): {}'.format(exc))
                    warned = True
            next_tick = max(next_tick + .1, time.monotonic())
            await asyncio.sleep(max(0, next_tick - time.monotonic()))

    async def command(self, payload, admin):
        cmd = payload.get('cmd') if isinstance(payload, dict) else None
        if not isinstance(cmd, str) or cmd not in ('preflight', 'start', 'estop', 'reset'):
            self.store.event('warning', 'cmd:unknown rejected(명령 형식 오류)')
            return
        accepted, reason = False, '관리자 연결만 허용'
        if admin:
            if self.world:
                if cmd == 'start' and any(r['blocking'] and not r['ok'] for r in self.state()['checklist']):
                    reason = 'blocking 점검 항목 실패'
                else:
                    accepted, reason = self.world.command(cmd)
            else:
                reason = 'M2 관찰 모드: 프로세스 제어는 M5에서 제공'
        text = 'cmd:{} {}'.format(cmd, 'accepted' if accepted else 'rejected(' + reason + ')')
        self.store.event('info' if accepted else 'warning', text)

    async def websocket(self, request):
        role = request.query.get('role', 'visitor')
        if role == 'admin' and request.remote not in ('127.0.0.1', '::1'):
            raise web.HTTPForbidden(text='관리자 소켓은 로컬 연결만 허용합니다')
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=20)
        await ws.prepare(request)
        slots, sender = SocketSlots(ws, len(self.cfg.drones),
                                     on_error=lambda text: self.store.event('warning', text)), None
        try:
            await asyncio.wait_for(ws.send_json(self.cfg.hello(self.mock)), 5)
            self.sockets.add(slots)
            sender = asyncio.create_task(slots.run())
            sender.add_done_callback(lambda task: self.sockets.discard(slots))
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except (json.JSONDecodeError, ValueError):
                        payload = None
                    await self.command(payload, role == 'admin')
        finally:
            self.sockets.discard(slots)
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def startup(self, app):
        if not self.mock:
            from dashboard.ros_io import ROSIO
            from dashboard.pinger import Pinger
            try:
                self.io = ROSIO(self.cfg, self.store)
                await self.io.start()
            except Exception as exc:
                self.store.unavailable(None, 'ros', 'ROS 시작 실패: {}'.format(exc))
            self.tasks.append(asyncio.create_task(Pinger(self.cfg, self.store).run()))
        self.tasks.append(asyncio.create_task(self.aggregate()))

    async def cleanup(self, app):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(s.ws.close() for s in tuple(self.sockets)), return_exceptions=True)
        if self.io:
            await self.io.close()

    def app(self):
        app = web.Application()
        app.router.add_get('/ws', self.websocket)
        async def health(request):
            return web.json_response(dict(milestone='M2', mock=self.mock, websocket='/ws',
                                           mode='mock' if self.mock else 'observation'))
        app.router.add_get('/', health)
        app.router.add_static('/static/', Path(__file__).resolve().parent / 'static', show_index=False)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='dashboard.yaml path')
    parser.add_argument('--field', help='field.yaml path')
    parser.add_argument('--mock', action='store_true')
    parser.add_argument('--mock-fail', action='store_true')
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--check-timeout', type=float, default=3)
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    cfg = load_config(args.config, args.field, mock=args.mock)
    host = args.host or cfg.raw.get('http', {}).get('host', '127.0.0.1')
    port = args.port if args.port is not None else cfg.raw.get('http', {}).get('port', 8080)
    print('ROS_DOMAIN_ID={} RMW_IMPLEMENTATION={} bind={}:{} mock={}'.format(
        os.environ.get('ROS_DOMAIN_ID', '(default)'), os.environ.get('RMW_IMPLEMENTATION', '(default)'),
        host, port, args.mock), flush=True)
    if args.check_config:
        from dashboard.ros_io import check_config
        rows = check_config(cfg, args.check_timeout)
        for row in rows:
            print('{}\t{}\t{}\t{}\t{}'.format(row.get('status', 'pass' if row['ok'] else 'fail').upper(), row['kind'],
                row['name'], row['expected_type'], row['detail']))
        for error in cfg.errors:
            print('FAIL\tconfiguration\t' + error)
        for warning in cfg.warnings:
            print('WARN\tconfiguration\t' + warning)
        return 1 if cfg.errors or any(not r['ok'] and r.get('status') != 'skip' for r in rows) else 0
    web.run_app(Dashboard(cfg, args.mock, args.mock_fail).app(), host=host, port=port)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
