// One visitor connection. Binary messages are intentionally discarded until M4.
export function connect({onHello, onState, onConnection}) {
  let socket = null;
  let retry = null;
  let stopped = false;
  let lastState = null;
  let socketOpenedAt = null;
  let socketLastState = null;
  let hasHello = false;
  let connected = false;
  let attempt = 0;
  function notify(value) {
    if (connected === value) return;
    connected = value;
    onConnection(value);
  }
  function open() {
    if (stopped) return;
    hasHello = false;
    socketOpenedAt = null;
    socketLastState = null;
    const address = new URL('/ws?role=visitor', location.href);
    address.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const current = new WebSocket(address);
    socket = current;
    current.binaryType = 'arraybuffer';
    current.onopen = () => {
      if (socket === current) socketOpenedAt = performance.now();
    };
    current.onmessage = event => {
      if (socket !== current || typeof event.data !== 'string') return;
      try {
        const value = JSON.parse(event.data);
        if (value.type === 'hello') {
          hasHello = true;
          onHello(value);
        } else if (value.type === 'state' && hasHello) {
          onState(value);
          lastState = performance.now();
          socketLastState = lastState;
          attempt = 0;
          notify(true);
        }
      } catch (error) {
        console.warn('대시보드 수신 처리 오류', error);
      }
    };
    current.onerror = () => current.close();
    current.onclose = () => {
      if (socket !== current || stopped) return;
      // Keep the visitor's last scene for the specified two-second grace.
      clearTimeout(retry);
      retry = setTimeout(open, Math.min(5000, 500 * 2 ** attempt++));
    };
  }
  onConnection(false);
  open();
  const watchdog = setInterval(() => {
    const now = performance.now();
    if (lastState !== null && now - lastState > 2000) {
      notify(false);
    }
    // Each new connection gets its own grace period, including its first state.
    // The previous scene's age must not close a freshly reconnected socket.
    const deadlineStart = socketLastState ?? socketOpenedAt;
    if (deadlineStart !== null && now - deadlineStart > 2000
        && socket?.readyState === WebSocket.OPEN) socket.close();
  }, 250);
  return () => {
    stopped = true;
    clearInterval(watchdog);
    clearTimeout(retry);
    socket?.close();
  };
}
