import {connect} from './ws.js';
import {present, makeNames, interests, activity, fixed, coordinate, navigationGoal} from './view-model.js';

const find = selector => document.querySelector(selector);
const root = find('.visitor');
const cameras = find('#cameras');
const fleet = find('#fleet');
const canvas = find('#field-canvas');
const text = (element, value) => { if (element.textContent !== value) element.textContent = value; };
let hello = null;
let state = null;
let connected = false;
let name = id => id;
let fieldRatio = 5 / 4;
let cameraNodes = new Map();
let robotNodes = new Map();

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function metric(label, unit = '') {
  const box = node('div');
  const value = node('span', 'metric-value', '—');
  const dd = node('dd');
  dd.append(value);
  if (unit) dd.append(node('span', 'unit', unit));
  box.append(node('dt', 'label', label), dd);
  return {box, value};
}

function configure(value) {
  hello = value;
  // A reconnect hello must not erase the previous state during the 2s grace.
  name = makeNames(hello);
  cameraNodes = new Map();
  robotNodes = new Map();
  cameras.replaceChildren();
  fleet.replaceChildren();
  const roles = [...hello.drones, ...hello.limos];
  fleet.style.setProperty('--role-count', Math.max(1, roles.length));
  for (const id of hello.drones) {
    const tile = node('article', 'camera-tile');
    tile.dataset.robot = id;
    tile.setAttribute('aria-label', `${name(id)} 카메라`);
    const img = node('img');
    img.src = '/static/mock/camera.jpg';
    img.alt = '카메라 자리 표시용 정적 영상';
    const title = node('div', 'camera-title');
    const badge = node('span', 'interest');
    badge.hidden = true;
    title.append(node('span', 'camera-name', name(id)), badge);
    const offline = node('span', 'camera-offline', '영상 연결 대기');
    offline.hidden = true;
    // M3 always uses the bundled placeholder, including a non-mock connection.
    tile.append(img, title, node('span', 'mock-badge', '샘플 영상'), offline);
    cameras.append(tile);
    cameraNodes.set(id, {tile, badge, offline});
  }
  for (const id of roles) {
    const drone = hello.drones.includes(id);
    const panel = node('article', 'robot-panel glass');
    panel.dataset.robot = id;
    panel.dataset.kind = drone ? 'drone' : 'limo';
    const task = node('p', 'robot-task', '다음 임무 대기');
    const values = node('dl', 'robot-values');
    const first = metric(drone ? '배터리' : '위치 (m)', drone ? 'V' : '');
    const second = metric(drone ? '고도' : '목표 (m)', drone ? 'm' : '');
    if (!drone) second.box.title = '마지막 이동 목표';
    values.append(first.box, second.box);
    panel.append(node('h3', '', name(id)), node('p', 'raw-name', id), task, values);
    fleet.append(panel);
    robotNodes.set(id, {panel, task, first: first.value, second: second.value});
  }
  const areas = [hello.field?.arena, hello.field?.limo_area].filter(Boolean);
  if (areas.length && areas.every(area => area.x?.length === 2 && area.y?.length === 2)) {
    const width = Math.max(...areas.map(area => area.x[1])) - Math.min(...areas.map(area => area.x[0]));
    const height = Math.max(...areas.map(area => area.y[1])) - Math.min(...areas.map(area => area.y[0]));
    if (width > 0 && height > 0) {
      fieldRatio = width / height;
      text(find('#field-size'), `${fixed(width, 0)} × ${fixed(height, 0)} m`);
    }
  }
  const idle = [...document.querySelectorAll('.idle-intro p')];
  idle.forEach((element, index) => text(element, hello.idle_lines?.[index] || ''));
  layout();
  render();
}

function layout() {
  const stage = find('.field-stage').getBoundingClientRect();
  const width = Math.min(stage.width, stage.height * fieldRatio);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${width / fieldRatio}px`;
  // No scene rendering in M3; the canvas is only a field-proportioned placeholder.
  canvas.width = Math.round(width * devicePixelRatio);
  canvas.height = Math.round(width / fieldRatio * devicePixelRatio);
  const count = cameraNodes.size;
  if (!count) return;
  const bounds = cameras.getBoundingClientRect();
  const gap = parseFloat(getComputedStyle(cameras).gap);
  // Two columns for the default four roles. Other configurations choose the best fit.
  let best = {width: 0, columns: 1, rows: count};
  for (let columns = 1; columns <= count; columns++) {
    const rows = Math.ceil(count / columns);
    const tileWidth = Math.min((bounds.width - gap * (columns - 1)) / columns,
      (bounds.height - gap * (rows - 1)) / rows * 324 / 244);
    if (tileWidth > best.width) best = {width: tileWidth, columns, rows};
  }
  cameras.style.gridTemplateColumns = `repeat(${best.columns}, ${best.width}px)`;
  cameras.style.gridTemplateRows = `repeat(${best.rows}, ${best.width * 244 / 324}px)`;
}

function render() {
  if (!hello) return;
  const view = present(hello, state, {connected, name});
  root.dataset.mode = view.mode;
  root.dataset.phase = view.phase || '';
  root.dataset.signal = view.signal;
  text(find('#phase-label'), view.label);
  text(find('#elapsed'), view.elapsed);
  text(find('#narrative'), view.sentence);
  find('.idle-intro').hidden = view.mode !== 'idle';
  find('.narrative').hidden = view.mode === 'idle';
  find('#connection-note').hidden = connected;
  const mission = state?.mission;
  const active = view.mode === 'active';
  text(find('#marker-value'), active && mission?.mission_marker_id != null ? `미션 ${mission.mission_marker_id}번` : '—');
  text(find('#target-value'), active ? coordinate(mission?.P_N) : '—');
  const rescue = {rescue_dispatch: '이동 중', rescue: '구조 중', return: '복귀 중', done: '구조 완료'};
  text(find('#rescue-value'), active ? rescue[view.phase] || (mission?.target_confirmed ? '위치 확인 완료' : '발견 대기') : '미션 대기');
  for (const [id, refs] of cameraNodes) {
    const robot = state?.robots?.[id];
    const ids = active ? interests(hello, state, id, robot?.detections) : [];
    text(refs.badge, ids.map(id => `${id}번`).join(' · '));
    refs.badge.hidden = !ids.length;
    refs.offline.hidden = connected;
  }
  for (const [id, refs] of robotNodes) {
    const robot = state?.robots?.[id];
    const drone = hello.drones.includes(id);
    const fresh = Number.isFinite(robot?.pose_age) && robot.pose_age <= (hello.freshness_s?.[drone ? 'pose' : 'odom'] ?? 1);
    text(refs.task, activity(hello, state, id, view));
    text(refs.first, drone ? fixed(robot?.battery_v, 2) : coordinate(fresh ? robot?.pose : null));
    // The last BT navigation command is reported; active Nav2 goal status is not.
    text(refs.second, drone ? fixed(fresh ? robot?.pose?.z : null, 2) : navigationGoal(state, id));
  }
}

if (new URLSearchParams(location.search).get('fx') === 'low') document.documentElement.dataset.fx = 'low';
new ResizeObserver(layout).observe(root);
const disconnect = connect({
  onHello: configure,
  onState(value) {state = value; render();},
  onConnection(value) {connected = value; render();},
});
window.addEventListener('pagehide', disconnect, {once: true});
