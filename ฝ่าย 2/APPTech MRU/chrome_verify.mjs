// Internal helper: stdout carries private cookies to Python, never to the user.
// Python owns Chrome's process group and temporary profile, including on timeout.
import {setTimeout as sleep} from 'node:timers/promises';

const origin = 'https://38rat.nstru.ac.th';
const port = Number(process.argv[2]);
const timeout = Number(process.argv[3]);
const deadline = Date.now() + timeout * 1000;
let socket;
let nextId = 0;
const pending = new Map();

function remaining() {
  const ms = deadline - Date.now();
  if (ms <= 0) throw new Error('Chrome verification timed out');
  return ms;
}

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('Chrome verification timed out'));
    }, remaining());
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error('Chrome page evaluation failed');
  return result.result.value;
}

try {
  if (typeof WebSocket !== 'function') throw new Error('Node.js 22+ is required');
  if (!Number.isInteger(port) || port <= 0 || !(timeout > 0)) {
    throw new Error('Invalid Chrome helper arguments');
  }
  let targets;
  while (!targets) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`, {
        signal: AbortSignal.timeout(Math.min(1000, remaining())),
      });
      targets = await response.json();
    } catch {
      remaining();
      await sleep(100);
    }
  }
  const target = targets.find(item => item.type === 'page');
  if (!target) throw new Error('Chrome did not create a page');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id);
      if (!item) return;
      clearTimeout(item.timer);
      pending.delete(message.id);
      if (message.error) item.reject(new Error('Chrome automation command failed'));
      else item.resolve(message.result);
    } else if (message.method === 'Fetch.requestPaused') {
      const request = message.params;
      const allowed = new URL(request.request.url).hostname === '38rat.nstru.ac.th'
        && !['Image', 'Media', 'Font'].includes(request.resourceType);
      // No external domains, image/media downloads, or browser-fingerprint patches.
      send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', allowed
        ? {requestId: request.requestId}
        : {requestId: request.requestId, errorReason: 'BlockedByClient'}
      ).catch(() => {});
    }
  });
  socket.addEventListener('close', () => {
    for (const item of pending.values()) {
      clearTimeout(item.timer);
      item.reject(new Error('Chrome disconnected'));
    }
    pending.clear();
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Chrome connection timed out')), remaining());
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, {once: true});
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Chrome connection failed')); }, {once: true});
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Browser.setDownloadBehavior', {behavior: 'deny'});
  await send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
  const navigation = await send('Page.navigate', {url: origin + '/'});
  if (navigation.errorText) throw new Error('Chrome could not load the MRU homepage');
  while (true) {
    remaining();
    await sleep(250);
    let state;
    try {
      state = await evaluate('({title:document.title,ready:document.readyState,host:location.hostname})');
    } catch {
      remaining(); // Navigation/reload can replace the execution context.
      continue;
    }
    if (state.host !== '38rat.nstru.ac.th' || state.ready !== 'complete'
        || !state.title || /one moment|just a moment|checking/i.test(state.title)) continue;
    const {cookies} = await send('Network.getCookies', {urls: [origin + '/']});
    if (!cookies.some(cookie => cookie.name === 'wssplashchk')) continue;
    const user_agent = await evaluate('navigator.userAgent');
    process.stdout.write(JSON.stringify({user_agent, cookies}));
    break;
  }
} catch (error) {
  process.stderr.write(error.message + '\n');
  process.exitCode = 1;
} finally {
  // Ask Chrome to shut down normally so its workers finish profile writes.
  if (socket?.readyState === WebSocket.OPEN) {
    try { await send('Browser.close'); } catch { /* Python provides bounded fallback cleanup. */ }
  }
  for (const item of pending.values()) {
    clearTimeout(item.timer);
    item.reject(new Error('Chrome helper closed'));
  }
  pending.clear();
  socket?.close();
}
