const userList = document.getElementById('userList');
const logList = document.getElementById('logList');
let logOffset = 0;
const logPageSize = 20;
let userOffset = 0;
const userPageSize = 20;

let canManageUsers = false;
let adminLoadFailed = false;
const userManageSection = document.getElementById('userManageSection');

function renderPageInfo(elementId, offset, pageSize, total) {
  const element = document.getElementById(elementId);
  if (!element) return;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(totalPages, Math.floor(offset / pageSize) + 1);
  element.textContent = `第 ${currentPage} / ${totalPages} 页 · 共 ${total} 条`;
}

if (!userList || !logList) {
  console.warn('admin.js: admin DOM 未就绪，跳过初始化');
}

async function syncPermission() {
  try {
    const res = await fetch('/api/account/me');

    if (!res.ok) {
      canManageUsers = false;
    } else {
      const data = await res.json();
      canManageUsers = !!data.user?.is_admin;
    }

  } catch (e) {
    canManageUsers = false;
  }
  const createBtn = document.getElementById('openCreateUser');
  if (createBtn) {
    createBtn.disabled = !canManageUsers;
    createBtn.title = canManageUsers ? '' : '普通用户无权限执行此操作';
  }

  if (userManageSection) {
    userManageSection.classList.toggle('d-none', !canManageUsers);
  }
}

async function refreshCameraStatus() {
  const el = document.getElementById('mCamera');
  if (!el) return;
  try {
    const res = await fetch('/api/camera/status');
    const data = await res.json();

    const phase = data.phase || (data.connected ? 'running' : 'offline');
    el.textContent = data.text || (phase === 'running' ? '已连接' : '离线');
    el.classList.toggle('status-online', phase === 'running');
    el.classList.toggle('status-ready', phase === 'ready');
    el.classList.toggle('status-offline', phase === 'offline');
  } catch (e) {
    el.textContent = '离线';
    el.classList.remove('status-online');
    el.classList.remove('status-ready');

    el.classList.add('status-offline');
  }

}

async function refreshAdmin() {
  const operator = document.getElementById('logOperator').value.trim();
  let data;
  try {
    const res = await fetch(`/api/admin/overview?offset=${logOffset}&limit=${logPageSize}&operator=${encodeURIComponent(operator)}`);
    if (!res.ok) {
      if (!adminLoadFailed) showToast('系统设置数据加载失败', 'danger');
      adminLoadFailed = true;
      return;
    }
    data = await res.json();
  } catch (e) {
    if (!adminLoadFailed) showToast('系统设置数据加载失败', 'danger');
    adminLoadFailed = true;
    return;
  }
  if (!data.ok) {
    if (!adminLoadFailed) showToast(data.message || '系统设置数据加载失败', 'danger');
    adminLoadFailed = true;
    return;
  }
  adminLoadFailed = false;

  animateNumber(document.getElementById('mUser'), data.metrics.user_count);
  animateNumber(document.getElementById('mHistory'), data.metrics.history_total);
  document.getElementById('mHistoryDesc').textContent = data.metrics.history_total_desc;
  animateNumber(document.getElementById('mLogs'), data.metrics.today_logs);

  userList.innerHTML = "";
  const userEmpty = document.getElementById("userEmpty");
  const allUsers = data.users || [];
  const userTotal = Number(data.pagination?.user_total ?? allUsers.length);
  const maxUserOffset = Math.max(0, (Math.max(1, Math.ceil(userTotal / userPageSize)) - 1) * userPageSize);
  userOffset = Math.min(userOffset, maxUserOffset);
  const effectivePagedUsers = allUsers.slice(userOffset, userOffset + userPageSize);
  if (userEmpty) userEmpty.classList.toggle("d-none", effectivePagedUsers.length > 0);
  effectivePagedUsers.forEach((u) => {
    const li = document.createElement('li');
    li.className = 'list-group-item';
    li.innerHTML = `<div><b>${u.username}</b> (${u.is_admin ? '管理员' : '普通用户'}) / ${u.status}</div>
      <div class='small text-muted'>邮箱: ${u.email || '-'} | 电话: ${u.phone || '-'} | 注册: ${u.created_at}</div>
      <div class='mt-1 d-flex gap-1 flex-wrap admin-user-actions'>
        <button class='btn btn-sm btn-outline-secondary'>查看</button>
        <button class='btn btn-sm btn-outline-warning' ${canManageUsers ? '' : 'disabled title="普通用户无权限"'}>修改密码</button>
        <button class='btn btn-sm btn-outline-info' ${canManageUsers ? '' : 'disabled title="普通用户无权限"'}>编辑</button>
        <button class='btn btn-sm btn-outline-danger' ${canManageUsers ? '' : 'disabled title="普通用户无权限"'}>删除</button>
      </div>`;
    const [viewBtn, pwdBtn, editBtn, delBtn] = li.querySelectorAll('button');
    viewBtn.onclick = () => {
      alert(`用户：${u.username}\n角色：${u.is_admin ? '管理员' : '普通用户'}\n状态：${u.status}\n邮箱：${u.email || '-'}\n电话：${u.phone || '-'}\n注册时间：${u.created_at}\n最近登录：${u.last_login_at || '-'}`);
    };
    pwdBtn.onclick = async () => {
      if (!canManageUsers) { showToast('普通用户无权限执行该操作', 'warning'); return; }
      const p = prompt(`请输入 ${u.username} 的新密码（至少6位）`, '12345678');
      if (!p) return;
      const r = await fetch(`/api/admin/users/${u.id}/password`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: p }) });
      showToast(r.ok ? '密码修改成功' : '密码修改失败', r.ok ? 'success' : 'danger');
    };
    editBtn.onclick = async () => {
      if (!canManageUsers) { showToast('普通用户无权限执行该操作', 'warning'); return; }
      const username = prompt('用户名', u.username);
      if (!username) return;
      const email = prompt('邮箱', u.email || '');
      const phone = prompt('电话', u.phone || '');
      const isAdmin = confirm('是否设为管理员？') ? true : false;
      const isActive = !confirm('是否禁用该账号？（选择“取消”保持启用）');
      const r = await fetch(`/api/admin/users/${u.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, email, phone, is_admin: isAdmin, is_active: isActive, avatar_url: u.avatar_url || '' }),
      });
      showToast(r.ok ? '用户更新成功' : '用户更新失败', r.ok ? 'success' : 'danger');
      refreshAdmin();
    };
    delBtn.onclick = async () => {
      if (!canManageUsers) { showToast('普通用户无权限执行该操作', 'warning'); return; }
      if (!confirm(`确认删除用户 ${u.username}？`)) return;
      const r = await fetch(`/api/admin/users/${u.id}`, { method: 'DELETE' });
      const d = await r.json();
      showToast(r.ok ? '用户已删除' : (d.message || '删除失败'), r.ok ? 'warning' : 'danger');
      refreshAdmin();
    };
    userList.appendChild(li);
  });

  const prevUserPageBtn = document.getElementById('prevUserPage');
  const nextUserPageBtn = document.getElementById('nextUserPage');
  if (prevUserPageBtn) prevUserPageBtn.disabled = userOffset <= 0;
  if (nextUserPageBtn) nextUserPageBtn.disabled = (userOffset + userPageSize) >= userTotal;
  renderPageInfo('userPageInfo', userOffset, userPageSize, userTotal);

  logList.innerHTML = '';
  data.logs.forEach((l) => {
    const li = document.createElement('li');
    li.className = 'list-group-item';
    li.textContent = `[${l.time}] ${l.operator} ${l.ip} ${l.content} (${l.result})`;
    logList.appendChild(li);
  });

  const logTotal = Number(data.pagination?.log_total ?? data.logs.length);
  const maxLogOffset = Math.max(0, (Math.max(1, Math.ceil(logTotal / logPageSize)) - 1) * logPageSize);
  if (logOffset > maxLogOffset) {
    logOffset = maxLogOffset;
    refreshAdmin();
    return;
  }
  document.getElementById('prevLogPage').disabled = logOffset <= 0;
  document.getElementById('nextLogPage').disabled = (logOffset + logPageSize) >= logTotal;
  renderPageInfo('logPageInfo', logOffset, logPageSize, logTotal);
}

document.getElementById('openCreateUser').onclick = async () => {
  if (!canManageUsers) {
    showToast('普通用户无权限执行该操作', 'warning');
    return;
  }
  const username = prompt('新用户名');
  if (!username) return;
  const password = prompt('初始密码（至少6位）', '12345678');
  if (!password) return;
  const email = prompt('邮箱', '');
  const phone = prompt('电话', '');
  const is_admin = confirm('是否设为管理员？');
  const res = await fetch('/api/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, email, phone, is_admin }),
  });
  const data = await res.json();
  showToast(res.ok ? "用户创建成功" : (data.message || "创建失败"), res.ok ? "success" : "danger");
  if (res.ok) refreshAdmin();
};

document.getElementById('saveCameraCfg').onclick = async () => {
  const payload = {
    resolution: document.getElementById('cfgRes').value,
    fps: Number(document.getElementById('cfgFps').value || 20),
    flip_horizontal: document.getElementById('cfgFlipH').checked,
    flip_vertical: document.getElementById('cfgFlipV').checked,
  };
  const res = await fetch('/api/camera/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (res.ok) showToast('摄像头配置已保存并实时生效');
};

async function loadCameraConfig() {
  try {
    const res = await fetch('/api/system/status');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.ok) return;
    const cfg = data.camera_settings || {};
  document.getElementById('cfgRes').value = cfg.resolution || '720P';
  document.getElementById('cfgFps').value = cfg.fps || 20;
  document.getElementById('cfgFlipH').checked = !!cfg.flip_horizontal;
  document.getElementById('cfgFlipV').checked = !!cfg.flip_vertical;
  } catch (e) {
    console.warn('loadCameraConfig failed', e);
  }
}

document.getElementById('logOperator').oninput = () => {
  logOffset = 0;
  refreshAdmin();
};
document.getElementById('prevLogPage').onclick = () => {
  logOffset = Math.max(0, logOffset - logPageSize);
  refreshAdmin();
};
document.getElementById('nextLogPage').onclick = () => {
  logOffset += logPageSize;
  refreshAdmin();
};
const prevUserPageBtn = document.getElementById('prevUserPage');
if (prevUserPageBtn) {
  prevUserPageBtn.onclick = () => {
    userOffset = Math.max(0, userOffset - userPageSize);
    refreshAdmin();
  };
}
const nextUserPageBtn = document.getElementById('nextUserPage');
if (nextUserPageBtn) {
  nextUserPageBtn.onclick = () => {
    userOffset += userPageSize;
    refreshAdmin();
  };
}


if (userList && logList) {
  syncPermission();
  refreshCameraStatus();
  refreshAdmin();
  loadCameraConfig();
  setInterval(() => refreshAdmin(), 5000);
  setInterval(refreshCameraStatus, 3000);
}
