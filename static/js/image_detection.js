const imageInput = document.getElementById('imageInput');
const imageDropZone = document.getElementById('imageDropZone');
const imagePreview = document.getElementById('imagePreview');
const imagePlaceholder = document.getElementById('imagePlaceholder');
const imageOverlay = document.getElementById('imageOverlay');
const imageCtx = imageOverlay.getContext('2d');
const detectionList = document.getElementById('imageDetectionList');
const analysisReport = document.getElementById('analysisReport');

let selectedFile = null;
let detectionBoxes = [];
let frameWidth = 0;
let frameHeight = 0;
let selectedBoxIndex = -1;
let previewUrl = '';

const labelNames = {
  Fissure: '条状缺陷',
  Crater: '坑状缺陷',
};

function displayLabel(label) {
  return labelNames[label] || label;
}

function resetAnalysis() {
  selectedBoxIndex = -1;
  document.getElementById('riskBadge').textContent = '未研判';
  document.getElementById('riskBadge').className = 'badge text-bg-secondary risk-badge';
  document.getElementById('analysisScopeText').textContent = '检测完成后，可研判全部区域或先点选某个检测框';
  analysisReport.className = 'analysis-report text-muted';
  analysisReport.textContent = '智能研判模块将结合缺陷类别、置信度、位置和框选面积，给出缺陷解释、风险等级、复核步骤与应对方案。';
}

function clearDetection() {
  detectionBoxes = [];
  frameWidth = 0;
  frameHeight = 0;
  selectedBoxIndex = -1;
  imageCtx.clearRect(0, 0, imageOverlay.width, imageOverlay.height);
  document.getElementById('detectionCountBadge').textContent = '0 个区域';
  document.getElementById('analyzeBtn').disabled = true;
  detectionList.innerHTML = '<div class="placeholder-panel" style="min-height: 220px;"><div>暂无检测结果</div><small>检测后可点击某个区域进行单独研判</small></div>';
  resetAnalysis();
}

function setSelectedFile(file) {
  if (!file) return;
  if (!file.type.startsWith('image/')) {
    showToast('请选择图片文件', 'warning');
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    showToast('图片不能超过 12MB', 'warning');
    return;
  }
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  selectedFile = file;
  previewUrl = URL.createObjectURL(file);
  imagePreview.src = previewUrl;
  imagePreview.classList.remove('d-none');
  imagePlaceholder.classList.add('d-none');
  document.getElementById('imageFileName').textContent = file.name;
  document.getElementById('imageMeta').textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB`;
  document.getElementById('detectImageBtn').disabled = false;
  document.getElementById('clearImageBtn').disabled = false;
  document.getElementById('imageDetectionStatus').textContent = '图片已就绪，点击开始检测';
  clearDetection();
}

function drawImageBoxes() {
  if (!imagePreview.naturalWidth || !frameWidth || !frameHeight) return;
  const width = imagePreview.clientWidth;
  const height = imagePreview.clientHeight;
  imageOverlay.width = width;
  imageOverlay.height = height;
  imageOverlay.style.width = `${width}px`;
  imageOverlay.style.height = `${height}px`;
  imageOverlay.style.left = `${imagePreview.offsetLeft}px`;
  imageOverlay.style.top = `${imagePreview.offsetTop}px`;
  imageCtx.clearRect(0, 0, width, height);

  const scaleX = width / frameWidth;
  const scaleY = height / frameHeight;
  imageCtx.font = '13px sans-serif';
  imageCtx.lineWidth = 2;
  detectionBoxes.forEach((box, index) => {
    const x = box.x * scaleX;
    const y = box.y * scaleY;
    const w = box.w * scaleX;
    const h = box.h * scaleY;
    const selected = index === selectedBoxIndex;
    imageCtx.strokeStyle = selected ? '#facc15' : '#00e1ff';
    imageCtx.lineWidth = selected ? 4 : 2;
    imageCtx.strokeRect(x, y, w, h);
    const text = `${displayLabel(box.label)} ${(box.conf * 100).toFixed(0)}%`;
    const textWidth = imageCtx.measureText(text).width + 10;
    imageCtx.fillStyle = selected ? 'rgba(133,77,14,.9)' : 'rgba(0,0,0,.72)';
    imageCtx.fillRect(x, Math.max(0, y - 22), textWidth, 22);
    imageCtx.fillStyle = '#fff';
    imageCtx.fillText(text, x + 5, Math.max(15, y - 7));
  });
}

function selectBox(index) {
  selectedBoxIndex = selectedBoxIndex === index ? -1 : index;
  document.querySelectorAll('.detection-result-item').forEach((item, itemIndex) => {
    item.classList.toggle('active', itemIndex === selectedBoxIndex);
  });
  document.getElementById('analysisScopeText').textContent = selectedBoxIndex >= 0
    ? `当前研判：区域 ${selectedBoxIndex + 1} · ${displayLabel(detectionBoxes[selectedBoxIndex].label)}`
    : '当前研判：全部检测区域';
  drawImageBoxes();
}

function renderDetectionResults() {
  document.getElementById('detectionCountBadge').textContent = `${detectionBoxes.length} 个区域`;
  detectionList.innerHTML = '';
  if (!detectionBoxes.length) {
    detectionList.innerHTML = '<div class="placeholder-panel" style="min-height: 220px;"><div>未检测到目标缺陷</div><small>可尝试上传更清晰或光照更均匀的图片</small></div>';
    return;
  }
  detectionBoxes.forEach((box, index) => {
    const item = document.createElement('div');
    item.className = 'detection-result-item';
    item.innerHTML = `
      <div class="d-flex justify-content-between align-items-center">
        <strong>区域 ${index + 1} · ${displayLabel(box.label)}</strong>
        <span class="badge text-bg-primary">${(box.conf * 100).toFixed(1)}%</span>
      </div>
      <div class="small text-muted mt-1">位置：(${box.x}, ${box.y})　尺寸：${box.w} × ${box.h}</div>
    `;
    item.onclick = () => selectBox(index);
    detectionList.appendChild(item);
  });
}

async function detectImage() {
  if (!selectedFile) return;
  const button = document.getElementById('detectImageBtn');
  button.disabled = true;
  button.textContent = '正在检测...';
  document.getElementById('imageDetectionStatus').textContent = '模型正在分析图片';
  const formData = new FormData();
  formData.append('image', selectedFile);
  try {
    const res = await fetch('/api/image-detection', { method: 'POST', body: formData });
    const data = await res.json().catch(() => ({ ok: false, message: '接口返回异常' }));
    if (handleAuthExpiryResponse(res, data)) return;
    if (!res.ok || !data.ok) throw new Error(data.message || '图片检测失败');
    detectionBoxes = data.boxes || [];
    frameWidth = data.frame_width || imagePreview.naturalWidth;
    frameHeight = data.frame_height || imagePreview.naturalHeight;
    selectedBoxIndex = -1;
    renderDetectionResults();
    drawImageBoxes();
    document.getElementById('imageDetectionStatus').textContent = detectionBoxes.length
      ? `检测完成，共发现 ${detectionBoxes.length} 个区域`
      : '检测完成，未发现目标缺陷';
    document.getElementById('imageModelMeta').textContent = `模型：${data.model_name || '-'}（${data.backend || 'OpenVINO'}）`;
    document.getElementById('imagePerfMeta').textContent = `推理耗时：${Number(data.inference_ms || 0).toFixed(2)}ms`;
    document.getElementById('analyzeBtn').disabled = false;
    document.getElementById('analysisScopeText').textContent = '当前研判：全部检测区域';
    showToast('图片检测完成');
  } catch (error) {
    document.getElementById('imageDetectionStatus').textContent = `检测失败：${error.message}`;
    showToast(error.message || '图片检测失败', 'danger');
  } finally {
    button.disabled = !selectedFile;
    button.textContent = '开始图片检测';
  }
}

async function analyzeDetection() {
  const button = document.getElementById('analyzeBtn');
  button.disabled = true;
  button.textContent = '正在研判...';
  analysisReport.className = 'analysis-report';
  analysisReport.textContent = '智能研判模型正在生成缺陷解释与处置方案，请稍候……';
  const boxes = selectedBoxIndex >= 0 ? [detectionBoxes[selectedBoxIndex]] : detectionBoxes;
  try {
    const res = await fetch('/api/intelligence/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ boxes, frame_width: frameWidth, frame_height: frameHeight }),
    });
    const data = await res.json().catch(() => ({ ok: false, message: '接口返回异常' }));
    if (handleAuthExpiryResponse(res, data)) return;
    if (!res.ok || !data.ok) throw new Error(data.message || '智能研判失败');
    analysisReport.textContent = data.report;
    const riskBadge = document.getElementById('riskBadge');
    riskBadge.textContent = data.risk_level || '已完成';
    const badgeClass = data.risk_level === '高'
      ? 'text-bg-danger'
      : (data.risk_level === '中' ? 'text-bg-warning' : (data.risk_level === '低' ? 'text-bg-success' : 'text-bg-secondary'));
    riskBadge.className = `badge ${badgeClass} risk-badge`;
    showToast(data.message || '智能研判完成');
  } catch (error) {
    analysisReport.textContent = `研判失败：${error.message}`;
    showToast(error.message || '智能研判失败', 'danger');
  } finally {
    button.disabled = false;
    button.textContent = '生成研判与处置方案';
  }
}

document.getElementById('chooseImageBtn').onclick = (event) => {
  event.stopPropagation();
  imageInput.click();
};
imageDropZone.onclick = () => imageInput.click();
imageInput.onchange = () => setSelectedFile(imageInput.files[0]);
imageDropZone.ondragover = (event) => {
  event.preventDefault();
  imageDropZone.classList.add('dragover');
};
imageDropZone.ondragleave = () => imageDropZone.classList.remove('dragover');
imageDropZone.ondrop = (event) => {
  event.preventDefault();
  imageDropZone.classList.remove('dragover');
  setSelectedFile(event.dataTransfer.files[0]);
};
document.getElementById('detectImageBtn').onclick = detectImage;
document.getElementById('analyzeBtn').onclick = analyzeDetection;
document.getElementById('clearImageBtn').onclick = () => {
  selectedFile = null;
  imageInput.value = '';
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = '';
  imagePreview.src = '';
  imagePreview.classList.add('d-none');
  imagePlaceholder.classList.remove('d-none');
  document.getElementById('imageFileName').textContent = '尚未选择图片';
  document.getElementById('imageMeta').textContent = '';
  document.getElementById('imageDetectionStatus').textContent = '等待上传图片';
  document.getElementById('detectImageBtn').disabled = true;
  document.getElementById('clearImageBtn').disabled = true;
  clearDetection();
};
imagePreview.onload = drawImageBoxes;
window.addEventListener('resize', drawImageBoxes);
