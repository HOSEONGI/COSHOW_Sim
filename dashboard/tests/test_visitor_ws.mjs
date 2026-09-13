import test from 'node:test';
import assert from 'node:assert/strict';
import { connect } from '../static/js/ws.js';

class Clock {
  now = 0;
  serial = 0;
  pending = new Map();
  schedule = (callback, delay, interval = false) => {
    const id = ++this.serial;
    this.pending.set(id, {callback, delay, at: this.now + delay, interval});
    return id;
  };
  cancel = id => this.pending.delete(id);
  advance(milliseconds) {
    const end = this.now + milliseconds;
    while (true) {
      const next = [...this.pending.entries()]
        .filter(([, task]) => task.at <= end)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      const [id, task] = next;
      this.now = task.at;
      if (task.interval) task.at += task.delay;
      else this.pending.delete(id);
      task.callback();
    }
    this.now = end;
  }
}

function environment(t) {
  const clock = new Clock();
  const sockets = [];
  const connections = [];
  const states = [];
  const hellos = [];
  class Socket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;
    readyState = Socket.CONNECTING;
    closes = 0;
    constructor(url) { this.url = String(url); sockets.push(this); }
    open() { this.readyState = Socket.OPEN; this.onopen?.({}); }
    message(value) { this.onmessage?.({data: JSON.stringify(value)}); }
    close() {
      if (this.readyState === Socket.CLOSED) return;
      this.closes++;
      this.readyState = Socket.CLOSED;
      this.onclose?.({});
    }
  }
  const replacements = {
    performance: {now: () => clock.now},
    location: {href: 'http://127.0.0.1:8080/visitor.html', protocol: 'http:'},
    WebSocket: Socket,
    setTimeout: (callback, delay) => clock.schedule(callback, delay),
    clearTimeout: clock.cancel,
    setInterval: (callback, delay) => clock.schedule(callback, delay, true),
    clearInterval: clock.cancel,
  };
  const originals = new Map(Object.keys(replacements)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(replacements))
    Object.defineProperty(globalThis, key, {value, configurable: true, writable: true});
  const stop = connect({onHello: value => hellos.push(value),
    onState: value => states.push(value), onConnection: value => connections.push(value)});
  t.after(() => {
    stop();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {clock, sockets, Socket, connections, states, hellos, stop};
}

for (const hello of [false, true]) {
  test(`initial open socket without state reconnects (hello=${hello})`, t => {
    const {clock, sockets, Socket, connections} = environment(t);
    const first = sockets[0];
    first.open();
    if (hello) first.message({type: 'hello'});
    clock.advance(2000);
    assert.equal(first.readyState, Socket.OPEN, 'allow the entire two-second grace');
    clock.advance(250);
    assert.equal(first.readyState, Socket.CLOSED, 'a first state is also subject to the watchdog');
    clock.advance(500);
    assert.equal(sockets.length, 2);
    assert.deepEqual(connections, [false]);
  });
}

test('newly opened reconnect gets its own state deadline despite old stale state', t => {
  const {clock, sockets, Socket, connections, states} = environment(t);
  const first = sockets[0];
  first.open();
  clock.advance(100);
  first.message({type: 'hello'});
  first.message({type: 'state', sequence: 1});
  first.close();
  clock.advance(500);
  const second = sockets[1];
  // Simulate a slow handshake while the previous scene becomes stale.
  clock.advance(2000);
  assert.deepEqual(connections, [false, true, false]);
  second.open();
  second.message({type: 'hello'});
  clock.advance(500);
  assert.equal(second.readyState, Socket.OPEN, 'old lastState must not immediately close this socket');
  second.message({type: 'state', sequence: 2});
  assert.deepEqual(states.map(value => value.sequence), [1, 2]);
  assert.equal(connections.at(-1), true);
});

test('a state received at monotonic zero still has a watchdog deadline', t => {
  const {clock, sockets, Socket, connections} = environment(t);
  const first = sockets[0];
  first.open();
  first.message({type: 'hello'});
  first.message({type: 'state'});
  clock.advance(2250);
  assert.equal(first.readyState, Socket.CLOSED);
  assert.deepEqual(connections, [false, true, false]);
});

test('hello is required again after reconnect; obsolete sockets cannot refresh it', t => {
  const {clock, sockets, states, connections} = environment(t);
  const first = sockets[0];
  first.open();
  first.message({type: 'hello'});
  first.message({type: 'state', sequence: 1});
  first.close();
  clock.advance(500);
  const second = sockets[1];
  second.open();
  second.message({type: 'state', sequence: 2});
  first.message({type: 'hello'});
  first.message({type: 'state', sequence: 3});
  assert.deepEqual(states.map(value => value.sequence), [1]);
  second.message({type: 'hello'});
  second.message({type: 'state', sequence: 4});
  assert.deepEqual(states.map(value => value.sequence), [1, 4]);
  assert.equal(connections.at(-1), true);
});

test('stop closes the socket and prevents watchdog or retry work', t => {
  const {clock, sockets, stop} = environment(t);
  sockets[0].open();
  sockets[0].close();
  stop();
  clock.advance(60000);
  assert.equal(sockets.length, 1);
  assert.equal(clock.pending.size, 0);
});
