const video = document.getElementById('video');
const serialImage = document.getElementById('serialImage');
const overlay = document.getElementById('overlay');
const ctx = overlay.getContext('2d');
const sourceType = document.getElementById('cameraSourceType');
const cameraDeviceSelect = document.getElementById('cameraDeviceSelect');
const cameraDeviceHint = document.getElementById('cameraDeviceHint');
const serialStatusText = document.getElementById('serialStatusText');
const countList = document.getElementById('countList');
const statusText = document.getElementById('statusText');
const cameraMeta = document.getElementById('cameraMeta');
const perfMeta = document.getElementById('perfMeta');
const modelMeta = document.getElementById('modelMeta');
const videoWrap = document.getElementById('videoWrap');
const monitorHudState = document.getElementById('monitorHudState');
const monitorHudLatency = document.getElementById('monitorHudLatency');

let stream = null;
let allVideoDevices = [];
let detectionTimer = null;
let serialFrameTimer = null;
let durationTimer = null;
let detectionRequestInFlight = false;
let durationBaseSeconds = 0;
let durationBaseAt = Date.now();
let durationCameraOn = false;
let lastBoxes = [];
let lastFrameSize = { width: 0, height: 0 };
let serialConnected = false;
let cameraStateOn = false;
let sourceSwitchInProgress = false;

const DETECTION_INTERVAL_MS = 350;
const SERIAL_FRAME_INTERVAL_MS = 180;
const DETECTION_FRAME_MAX_SIZE = 640;
const USB_CAMERA_KEYWORDS = /usb|external|logitech|webcam|uvc|capture|外接|camera\s*2|摄像头\s*2/i;
const BUILTIN_CAMERA_KEYWORDS = /integrated|built.?in|internal|facetime|front|内置/i;

async function postApi(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: payload ? JSON.stringify(payload) : null,
  });
  const data = await response.json().catch(() => ({ ok: false, message: '接口返回异常' }));
  if (handleAuthExpiryResponse(response, data)) return { ok: false, message: '登录已失效' };
  if (!response.ok) return { ok: false, message: data.message || `请求失败(${response.status})` };
  return data;
}

function activePreviewElement() {
  return sourceType.value === 'serial' ? serialImage : video;
}

function formatDuration(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const h = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function renderDurationTick() {
  const extra = durationCameraOn ? Math.max(0, Math.floor((Date.now() - durationBaseAt) / 1000)) : 0;
  document.getElementById('todayDuration').textContent = formatDuration((durationBaseSeconds + extra) * 1000);
}

function syncDuration(baseSeconds = 0, cameraOn = false) {
  const nextBase = Math.max(0, Number(baseSeconds || 0));
  if (Math.abs(nextBase - durationBaseSeconds) >= 2 || durationCameraOn !== !!cameraOn) {
    durationBaseSeconds = nextBase;
    durationBaseAt = Date.now();
  }
  durationCameraOn = !!cameraOn;
  renderDurationTick();
  if (!durationTimer) durationTimer = setInterval(renderDurationTick, 1000);
}

function stopBrowserStream() {
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  video.srcObject = null;
}

function classifyVideoDevice(device, index) {
  const label = (device.label || '').toLowerCase();
  if (USB_CAMERA_KEYWORDS.test(label)) return 'usb';
  if (BUILTIN_CAMERA_KEYWORDS.test(label)) return 'builtin';
  return index > 0 ? 'usb' : 'unknown';
}

function cameraDeviceCandidates() {
  const indexedDevices = allVideoDevices.map((device, index) => ({
    device,
    index,
    type: classifyVideoDevice(device, index),
  }));
  if (sourceType.value === 'usb') {
    const usbDevices = indexedDevices.filter((item) => item.type === 'usb');
    if (usbDevices.length) {
      return {
        devices: usbDevices,
        preferredDeviceId: usbDevices[0].device.deviceId,
        hint: `已发现 ${usbDevices.length} 个 USB/外接摄像头。`,
      };
    }
    if (indexedDevices.length > 1) {
      return {
        devices: indexedDevices,
        preferredDeviceId: indexedDevices[1].device.deviceId,
        hint: '无法从名称区分 USB 摄像头，已列出全部视频设备，请手动选择外接设备。',
      };
    }
    if (indexedDevices.length === 1 && indexedDevices[0].type === 'unknown') {
      return {
        devices: indexedDevices,
        preferredDeviceId: indexedDevices[0].device.deviceId,
        hint: '仅发现 1 个视频设备，将按 USB 摄像头尝试打开。',
      };
    }
    return {
      devices: [],
      preferredDeviceId: '',
      hint: '未发现 USB 外接摄像头，请检查设备连接、浏览器权限或设备是否被占用。',
    };
  }
  const builtinDevices = indexedDevices.filter((item) => item.type === 'builtin' || item.type === 'unknown');
  if (builtinDevices.length) {
    return {
      devices: builtinDevices,
      preferredDeviceId: builtinDevices[0].device.deviceId,
      hint: `已发现 ${builtinDevices.length} 个电脑内置/默认摄像头。`,
    };
  }
  return {
    devices: indexedDevices.slice(0, 1),
    preferredDeviceId: indexedDevices[0]?.device.deviceId || '',
    hint: indexedDevices.length
      ? '未识别到内置摄像头，已使用浏览器默认视频设备。'
      : '未发现电脑内置摄像头，请检查设备连接和浏览器权限。',
  };
}

function cameraDeviceLabel(item, optionIndex) {
  const label = (item.device.label || '').trim();
  if (label) return label;
  if (sourceType.value === 'usb' && item.index > 0) return `USB 摄像头 ${optionIndex + 1}`;
  return `${sourceType.value === 'usb' ? '视频设备' : '内置摄像头'} ${optionIndex + 1}`;
}

function renderCameraDevices() {
  const { devices, preferredDeviceId, hint } = cameraDeviceCandidates();
  const previous = cameraDeviceSelect.value;
  cameraDeviceSelect.innerHTML = '';
  if (!devices.length) {
    const label = sourceType.value === 'usb' ? '未发现 USB 外接摄像头' : '未发现电脑内置摄像头';
    const option = document.createElement('option');
    option.value = '';
    option.textContent = label;
    option.disabled = true;
    option.selected = true;
    cameraDeviceSelect.appendChild(option);
    cameraDeviceHint.textContent = hint || `${label}，请检查设备连接和浏览器权限。`;
    return;
  }
  devices.forEach((item, index) => {
    const option = document.createElement('option');
    option.value = item.device.deviceId;
    option.dataset.rawLabel = cameraDeviceLabel(item, index);
    option.dataset.deviceIndex = String(item.index);
    option.textContent = option.dataset.rawLabel;
    cameraDeviceSelect.appendChild(option);
  });
  if ([...cameraDeviceSelect.options].some((option) => option.value === previous)) {
    cameraDeviceSelect.value = previous;
  } else if (preferredDeviceId) {
    cameraDeviceSelect.value = preferredDeviceId;
  }
  cameraDeviceHint.textContent = hint;
}

async function enumerateCameras(requestPermission = false) {
  if (!navigator.mediaDevices?.enumerateDevices) {
    cameraDeviceHint.textContent = '当前浏览器不支持摄像头设备枚举。';
    return;
  }
  let permissionStream = null;
  if (requestPermission && !stream) {
    permissionStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
  }
  allVideoDevices = (await navigator.mediaDevices.enumerateDevices())
    .filter((device) => device.kind === 'videoinput');
  permissionStream?.getTracks().forEach((track) => track.stop());
  renderCameraDevices();
}

function currentCameraLabel() {
  const option = cameraDeviceSelect.selectedOptions[0];
  return option?.dataset.rawLabel || option?.textContent || '浏览器摄像头';
}

async function ensureBrowserCameraSelection(type) {
  if (cameraDeviceSelect.value) return true;
  await enumerateCameras(true);
  if (cameraDeviceSelect.value) return true;
  if (type === 'usb') {
    showToast('无法锁定 USB 摄像头，请点击扫描摄像头后从列表中选择外接设备', 'warning');
    return false;
  }
  return true;
}

function cameraConstraints() {
  const resolution = document.getElementById('cfgResolution').value;
  const fps = Number(document.getElementById('cfgFps').value || 20);
  const sizeMap = {
    VGA: { width: 640, height: 480 },
    '720P': { width: 1280, height: 720 },
    '1080P': { width: 1920, height: 1080 },
  };
  const size = sizeMap[resolution] || sizeMap['720P'];
  return {
    deviceId: cameraDeviceSelect.value ? { exact: cameraDeviceSelect.value } : undefined,
    width: { ideal: size.width },
    height: { ideal: size.height },
    frameRate: { ideal: fps, max: fps },
  };
}

function applyMirrorTransform() {
  const sx = document.getElementById('cfgFlipH').checked ? -1 : 1;
  const sy = document.getElementById('cfgFlipV').checked ? -1 : 1;
  const transform = `scale(${sx}, ${sy})`;
  video.style.transform = transform;
  serialImage.style.transform = transform;
  overlay.style.transform = transform;
}

function updateSourcePanels() {
  const serialMode = sourceType.value === 'serial';
  document.getElementById('browserCameraPanel').classList.toggle('d-none', serialMode);
  document.getElementById('serialDevicePanel').classList.toggle('d-none', !serialMode);
  video.classList.toggle('d-none', serialMode);
  serialImage.classList.toggle('d-none', !serialMode);
  document.getElementById('serialWaiting').classList.toggle('d-none', !serialMode || !!serialImage.src);
  if (!serialMode) renderCameraDevices();
  drawBoxes();
}

async function scanSerialPorts() {
  const response = await fetch('/api/device/ports');
  const data = await response.json();
  const select = document.getElementById('serialPortSelect');
  select.innerHTML = '';
  if (!data.ok || !data.ports?.length) {
    select.innerHTML = '<option value="">未发现可用串口</option>';
    serialStatusText.textContent = '未发现串口设备，请检查 USB 转串口驱动和设备连接。';
    return;
  }
  data.ports.forEach((port) => {
    const option = document.createElement('option');
    option.value = port.device;
    option.textContent = `${port.device} — ${port.description || '串口设备'}`;
    select.appendChild(option);
  });
  serialStatusText.textContent = `已发现 ${data.ports.length} 个串口设备。`;
}

async function connectSerialDevice() {
  const port = document.getElementById('serialPortSelect').value;
  const baudrate = Number(document.getElementById('serialBaudrate').value || 115200);
  const data = await postApi('/api/device/connect', { port, baudrate, timeout_ms: 300 });
  if (!data.ok) {
    showToast(data.message || '串口连接失败', 'danger');
    return;
  }
  serialConnected = true;
  serialStatusText.textContent = `已连接 ${port}，波特率 ${baudrate}，等待设备数据。`;
  startSerialFramePolling();
  showToast('设备通信模块连接成功');
}

async function disconnectSerialDevice() {
  const data = await postApi('/api/device/disconnect');
  if (!data.ok) {
    showToast(data.message || '设备断开失败', 'danger');
    return;
  }
  serialConnected = false;
  stopSerialFramePolling();
  serialImage.removeAttribute('src');
  document.getElementById('serialWaiting').classList.remove('d-none');
  serialStatusText.textContent = '设备通信模块已断开。';
  ensureDetectionPolling(false);
  showToast('设备通信模块已断开', 'secondary');
}

async function sendSerialCommand() {
  const command = document.getElementById('serialCommand').value.trim();
  const data = await postApi('/api/device/command', { command });
  if (!data.ok) {
    showToast(data.message || '指令发送失败', 'danger');
    return;
  }
  serialStatusText.textContent = `已发送：${data.command}（${data.bytes} 字节）`;
  showToast('设备指令已发送');
}

async function pollSerialFrame() {
  if (!serialConnected || sourceType.value !== 'serial') return;
  try {
    const response = await fetch('/api/device/frame');
    const data = await response.json();
    if (!response.ok || !data.ok) {
      serialStatusText.textContent = data.message || '串口读取失败';
      return;
    }
    if (data.waiting) {
      const reply = data.messages?.length ? `；设备回复：${data.messages.at(-1)}` : '';
      serialStatusText.textContent = `${data.message}；缓冲区 ${data.buffer_bytes || 0} 字节${reply}`;
      return;
    }
    serialImage.src = `data:image/jpeg;base64,${data.frame}`;
    document.getElementById('serialWaiting').classList.add('d-none');
    const reply = data.messages?.length ? `；设备回复：${data.messages.at(-1)}` : '';
    serialStatusText.textContent = `已接收图像帧：${data.frame_bytes} 字节；最近接收 ${data.rx_bytes} 字节${reply}`;
  } catch (error) {
    serialStatusText.textContent = '设备通信连接异常';
  }
}

function startSerialFramePolling() {
  if (!serialFrameTimer) {
    serialFrameTimer = setInterval(pollSerialFrame, SERIAL_FRAME_INTERVAL_MS);
    pollSerialFrame();
  }
}

function stopSerialFramePolling() {
  if (serialFrameTimer) clearInterval(serialFrameTimer);
  serialFrameTimer = null;
}

async function openSelectedSource() {
  const type = sourceType.value;
  if (type === 'serial') {
    if (!serialConnected) {
      showToast('请先连接设备通信模块', 'warning');
      return;
    }
    stopBrowserStream();
    startSerialFramePolling();
    const response = await postApi('/api/camera/start', {
      camera_type: 'serial',
      camera_label: `串口视觉设备 ${document.getElementById('serialPortSelect').value}`,
    });
    if (!response.ok) {
      showToast(response.message || '采集源开启失败', 'danger');
      return;
    }
    cameraStateOn = true;
    statusText.textContent = '状态：串口采集已开启，等待开始检测';
    await refreshSystem();
    return;
  }

  stopBrowserStream();
  try {
    if (!(await ensureBrowserCameraSelection(type))) return;
    stream = await navigator.mediaDevices.getUserMedia({ video: cameraConstraints(), audio: false });
    video.srcObject = stream;
    await video.play();
    applyMirrorTransform();
    const trackLabel = (stream.getVideoTracks()[0]?.label || '').trim();
    const selectedOption = cameraDeviceSelect.selectedOptions[0];
    if (trackLabel && selectedOption) {
      selectedOption.dataset.rawLabel = trackLabel;
      selectedOption.textContent = trackLabel;
    }
    const response = await postApi('/api/camera/start', {
      camera_type: type,
      camera_label: trackLabel || currentCameraLabel(),
    });
    if (!response.ok) throw new Error(response.message || '服务端状态开启失败');
    cameraStateOn = true;
    statusText.textContent = '状态：摄像头已开启，等待开始检测';
    showToast(type === 'usb' ? 'USB 摄像头已开启' : '电脑内置摄像头已开启');
    await refreshSystem();
  } catch (error) {
    stopBrowserStream();
    showToast(`摄像头打开失败：${error.message || '请检查权限或设备占用'}`, 'danger');
  }
}

function drawBoxes(boxes = lastBoxes, frameWidth = lastFrameSize.width, frameHeight = lastFrameSize.height) {
  const target = activePreviewElement();
  const width = target.clientWidth || 1;
  const height = target.clientHeight || 1;
  overlay.width = width;
  overlay.height = height;
  ctx.clearRect(0, 0, width, height);
  if (!frameWidth || !frameHeight) return;
  const scale = Math.min(width / frameWidth, height / frameHeight);
  const offsetX = (width - frameWidth * scale) / 2;
  const offsetY = (height - frameHeight * scale) / 2;
  ctx.lineWidth = 2;
  ctx.font = '13px sans-serif';
  boxes.forEach((box) => {
    const x = offsetX + box.x * scale;
    const y = offsetY + box.y * scale;
    const w = box.w * scale;
    const h = box.h * scale;
    ctx.strokeStyle = '#00e1ff';
    ctx.fillStyle = 'rgba(0,0,0,.65)';
    ctx.strokeRect(x, y, w, h);
    const text = `${box.label} ${(box.conf * 100).toFixed(0)}%`;
    const textWidth = ctx.measureText(text).width + 10;
    ctx.fillRect(x, Math.max(0, y - 22), textWidth, 22);
    ctx.fillStyle = '#fff';
    ctx.fillText(text, x + 5, Math.max(15, y - 7));
  });
}

function renderCounts(counts = {}) {
  countList.innerHTML = '';
  const entries = Object.entries(counts);
  if (!entries.length) {
    countList.innerHTML = '<li class="list-group-item">暂无检测数据</li>';
    return;
  }
  entries.forEach(([label, count]) => {
    const item = document.createElement('li');
    item.className = 'list-group-item d-flex justify-content-between';
    item.innerHTML = `<span>${label}</span><strong>${count}</strong>`;
    countList.appendChild(item);
  });
}

async function captureDetectionPayload() {
  if (sourceType.value === 'serial') {
    return serialImage.src?.startsWith('data:image') ? { frame: serialImage.src } : null;
  }
  if (!stream || !video.videoWidth || !video.videoHeight) return null;
  const canvas = document.createElement('canvas');
  const scale = Math.min(1, DETECTION_FRAME_MAX_SIZE / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
  canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  return { frame: canvas.toDataURL('image/jpeg', 0.68) };
}

async function pollDetection() {
  if (detectionRequestInFlight) return;
  const payload = await captureDetectionPayload();
  if (!payload) return;
  detectionRequestInFlight = true;
  try {
    const data = await postApi('/api/detection/frame-data', payload);
    if (!data.ok) {
      statusText.textContent = `状态：${data.message || '检测失败'}`;
      return;
    }
    lastBoxes = data.boxes || [];
    lastFrameSize = { width: data.frame_width || 0, height: data.frame_height || 0 };
    drawBoxes();
    renderCounts(data.counts);
    statusText.textContent = '状态：实时检测中';
    perfMeta.textContent = `推理耗时：${Number(data.inference_ms || 0).toFixed(2)}ms`;
    if (monitorHudLatency) {
      monitorHudLatency.textContent = `${Number(data.inference_ms || 0).toFixed(1)} ms`;
    }
  } finally {
    detectionRequestInFlight = false;
  }
}

function ensureDetectionPolling(enabled) {
  if (enabled && !detectionTimer) {
    detectionTimer = setInterval(pollDetection, DETECTION_INTERVAL_MS);
    pollDetection();
  }
  if (!enabled && detectionTimer) clearInterval(detectionTimer);
  if (!enabled) {
    detectionTimer = null;
    lastBoxes = [];
    drawBoxes([]);
  }
}

async function refreshSystem() {
  try {
    const [statusResponse, statsResponse] = await Promise.all([
      fetch('/api/system/status'),
      fetch('/api/stats/live'),
    ]);
    const data = await statusResponse.json();
    const stats = await statsResponse.json();
    if (!data.ok) return;
    cameraStateOn = !!data.camera_on;
    serialConnected = !!data.machine_connected;
    const serialOccupied = !!data.machine_occupied;
    const connectSerialBtn = document.getElementById('connectSerialBtn');
    const disconnectSerialBtn = document.getElementById('disconnectSerialBtn');
    const sendSerialCommandBtn = document.getElementById('sendSerialCommandBtn');
    if (connectSerialBtn) connectSerialBtn.disabled = serialOccupied;
    if (disconnectSerialBtn) disconnectSerialBtn.disabled = !serialConnected;
    if (sendSerialCommandBtn) sendSerialCommandBtn.disabled = !serialConnected;
    if (!sourceSwitchInProgress) {
      if (
        data.camera_on
        && ['builtin', 'usb', 'serial'].includes(data.camera_type)
        && sourceType.value !== data.camera_type
      ) {
        sourceType.value = data.camera_type;
        updateSourcePanels();
      } else if (
        data.camera_on
        && serialConnected
        && data.camera_type === 'serial'
        && sourceType.value !== 'serial'
      ) {
        sourceType.value = 'serial';
        updateSourcePanels();
      }
    }
    if (serialConnected) {
      startSerialFramePolling();
      serialStatusText.textContent = `设备通信模块已连接：${data.machine_settings?.port || '-'} @ ${data.machine_settings?.baudrate || '-'}。`;
    } else {
      stopSerialFramePolling();
      if (serialOccupied) {
        serialStatusText.textContent = `设备正由 ${data.machine_owner || '其他用户'} 使用，释放后当前账号才能连接。`;
      }
    }
    document.getElementById('cameraStateMini').textContent = data.camera_on ? '在线' : '离线';
    statusText.textContent = `状态：${data.detection_on ? '实时检测中' : (data.camera_on ? '采集源已开启' : '待机')}`;
    if (videoWrap) {
      videoWrap.classList.toggle('ui-camera-live', !!data.camera_on);
      videoWrap.classList.toggle('ui-detection-live', !!data.camera_on && !!data.detection_on);
    }
    if (monitorHudState) {
      const hudLabel = data.detection_on ? 'DETECTING' : (data.camera_on ? 'READY' : 'STANDBY');
      const hudText = monitorHudState.querySelector('b');
      if (hudText) hudText.textContent = hudLabel;
      monitorHudState.classList.toggle('is-live', !!data.camera_on);
      monitorHudState.classList.toggle('is-detecting', !!data.detection_on);
    }
    if (monitorHudLatency && !data.detection_on) {
      monitorHudLatency.textContent = '-- ms';
    }
    const config = data.camera_settings || {};
    cameraMeta.textContent = `设备：${data.camera_label || '-'} | 分辨率：${config.resolution || '-'} | 帧率：${config.fps || '-'}fps`;
    modelMeta.textContent = `模型：${data.model_name || '-'}（${data.model_backend || '-'} / ${data.model_loaded ? '已加载' : '未加载'}）`;
    document.getElementById('cfgResolution').value = config.resolution || '720P';
    document.getElementById('cfgFps').value = config.fps || 20;
    document.getElementById('cfgFlipH').checked = !!config.flip_horizontal;
    document.getElementById('cfgFlipV').checked = !!config.flip_vertical;
    const machine = data.machine_settings || {};
    if (machine.port && !document.getElementById('serialPortSelect').value) {
      const option = document.createElement('option');
      option.value = machine.port;
      option.textContent = machine.port;
      document.getElementById('serialPortSelect').appendChild(option);
      document.getElementById('serialPortSelect').value = machine.port;
    }
    document.getElementById('serialBaudrate').value = String(machine.baudrate || 115200);
    applyMirrorTransform();
    syncDuration(data.today_detection_seconds || 0, data.camera_on);
    document.getElementById('onlineUsers').textContent = stats.cards?.active_users || 0;
    const previewReady = sourceType.value === 'serial' ? !!serialImage.src : !!stream;
    ensureDetectionPolling(data.camera_on && data.detection_on && previewReady);
  } catch (error) {
    statusText.textContent = '状态：服务连接异常';
  }
}

sourceType.onchange = async () => {
  sourceSwitchInProgress = true;
  ensureDetectionPolling(false);
  stopBrowserStream();
  try {
    if (cameraStateOn) {
      const result = await postApi('/api/camera/stop');
      if (result.ok) cameraStateOn = false;
    }
    updateSourcePanels();
    if (sourceType.value !== 'serial') await enumerateCameras(false);
  } finally {
    sourceSwitchInProgress = false;
  }
};
document.getElementById('refreshCameraBtn').onclick = async () => {
  try {
    await enumerateCameras(true);
    showToast('摄像头设备列表已刷新');
  } catch (error) {
    showToast('无法获取摄像头权限', 'warning');
  }
};
document.getElementById('scanSerialBtn').onclick = scanSerialPorts;
document.getElementById('connectSerialBtn').onclick = connectSerialDevice;
document.getElementById('disconnectSerialBtn').onclick = disconnectSerialDevice;
document.getElementById('sendSerialCommandBtn').onclick = sendSerialCommand;
document.getElementById('openCameraBtn').onclick = openSelectedSource;
document.getElementById('closeCameraBtn').onclick = async () => {
  ensureDetectionPolling(false);
  stopBrowserStream();
  const result = await postApi('/api/camera/stop');
  if (result.ok) cameraStateOn = false;
  renderCounts({});
  statusText.textContent = '状态：采集源已关闭';
  showToast('采集源已关闭', 'secondary');
  await refreshSystem();
};
document.getElementById('startDetBtn').onclick = async () => {
  const ready = sourceType.value === 'serial' ? serialConnected : !!stream;
  if (!ready) {
    showToast('请先打开采集源', 'warning');
    return;
  }
  const data = await postApi('/api/detection/start');
  if (!data.ok) {
    showToast(data.message || '检测开启失败', 'warning');
    return;
  }
  ensureDetectionPolling(true);
};
document.getElementById('stopDetBtn').onclick = async () => {
  await postApi('/api/detection/stop');
  ensureDetectionPolling(false);
  statusText.textContent = '状态：检测已停止，采集源保持开启';
};
document.getElementById('applyCameraCfgBtn').onclick = async () => {
  const payload = {
    resolution: document.getElementById('cfgResolution').value,
    fps: Number(document.getElementById('cfgFps').value || 20),
    flip_horizontal: document.getElementById('cfgFlipH').checked,
    flip_vertical: document.getElementById('cfgFlipV').checked,
  };
  const result = await postApi('/api/camera/settings', payload);
  if (!result.ok) {
    showToast(result.message || '参数保存失败', 'danger');
    return;
  }
  applyMirrorTransform();
  if (stream) await openSelectedSource();
  showToast('采集参数已应用');
};
document.getElementById('fullscreenBtn').onclick = async () => {
  const wrap = document.getElementById('videoWrap');
  if (!document.fullscreenElement) await wrap.requestFullscreen();
  else await document.exitFullscreen();
};

serialImage.onload = drawBoxes;
window.addEventListener('resize', drawBoxes);
window.addEventListener('beforeunload', stopBrowserStream);
navigator.mediaDevices?.addEventListener?.('devicechange', () => enumerateCameras(false));

updateSourcePanels();
enumerateCameras(false).catch(() => {});
scanSerialPorts().catch(() => {});
refreshSystem();
renderDurationTick();
setInterval(refreshSystem, 2500);
