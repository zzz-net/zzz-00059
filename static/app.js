const API = '/api';

let CURRENT_IS_APPROVER = false;
let APPROVER_LIST = [];

const STATUS_MAP = {
    'submitted': '已提交',
    'pending_approval': '待审批',
    'confirmed': '已确认',
    'cancelled': '已取消',
    'rejected': '已驳回'
};

const PRECHECK_LABEL = {
    'pass': { text: '预检：预计可通过', cls: 'precheck-pass' },
    'warning': { text: '预检：有待审批重叠', cls: 'precheck-warning' },
    'conflict': { text: '预检：存在已确认冲突', cls: 'precheck-danger' },
    'quota_exceeded': { text: '预检：配额已满', cls: 'precheck-danger' },
    'not_applicable': { text: '', cls: '' }
};

const ACTION_MAP = {
    'submit': '提交申请',
    'auto_route': '系统流转',
    'approve': '审批通过',
    'reject': '审批驳回',
    'cancel': '取消申请',
    'revoke_cancel': '撤销取消'
};

function getOperator() {
    return document.getElementById('currentOperator').value.trim() || '匿名';
}

async function refreshRole() {
    const name = getOperator();
    try {
        const info = await apiGet('/auth/info?name=' + encodeURIComponent(name));
        CURRENT_IS_APPROVER = info.is_approver;
        APPROVER_LIST = info.approvers || [];

        const badge = document.getElementById('roleBadge');
        const hint = document.getElementById('roleHint');
        const approvalBtns = document.querySelectorAll('.tab-btn[data-tab="approval"]');
        const approvalPanel = document.getElementById('tab-approval');
        if (CURRENT_IS_APPROVER) {
            badge.textContent = '审批人';
            badge.className = 'status-badge status-confirmed';
            hint.textContent = '';
            approvalBtns.forEach(b => b.style.display = '');
            if (approvalPanel) approvalPanel.style.display = '';
        } else {
            badge.textContent = '普通申请人';
            badge.className = 'status-badge role-badge-applicant';
            hint.textContent = '（审批人: ' + APPROVER_LIST.join('、') + '）';
            approvalBtns.forEach(b => b.style.display = 'none');
            const activeApprovalBtn = document.querySelector('.tab-btn.active[data-tab="approval"]');
            if (activeApprovalBtn || (approvalPanel && approvalPanel.classList.contains('active'))) {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
                const firstBtn = document.querySelector('.tab-btn[data-tab="venues"]');
                const firstPanel = document.getElementById('tab-venues');
                if (firstBtn) firstBtn.classList.add('active');
                if (firstPanel) firstPanel.classList.add('active');
                loadVenues();
                return;
            }
            if (approvalPanel) approvalPanel.style.display = 'none';
        }

        const activeTab = document.querySelector('.tab-btn.active');
        if (activeTab) {
            const tab = activeTab.dataset.tab;
            if (tab === 'venues') loadVenues();
            if (tab === 'applications') loadApplications();
            if (tab === 'approval') loadApprovalList();
            if (tab === 'schedule') loadSchedule();
            if (tab === 'logs') loadAuditLogs();
        }
    } catch (e) {
        console.error('刷新角色失败', e);
    }
}

async function apiGet(url) {
    const res = await fetch(API + url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
}

async function apiPost(url, body) {
    const res = await fetch(API + url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
}

async function apiPut(url, body) {
    const res = await fetch(API + url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
}

async function apiDelete(url) {
    const res = await fetch(API + url, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (tab === 'approval' && !CURRENT_IS_APPROVER) {
            alert('无权访问审批面板，需审批人权限');
            return;
        }
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + tab).classList.add('active');

        if (tab === 'venues') loadVenues();
        if (tab === 'applications') loadApplications();
        if (tab === 'approval') loadApprovalList();
        if (tab === 'schedule') loadSchedule();
        if (tab === 'logs') loadAuditLogs();
    });
});

function loadVenues() {
    apiGet('/venues').then(venues => {
        const container = document.getElementById('venueList');
        if (venues.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无场地，点击右上角新增</div>';
            return;
        }
        container.innerHTML = venues.map(v => `
            <div class="venue-card">
                <h3>${escapeHtml(v.name)} ${v.is_active ? '' : '<span class="status-badge inactive-badge">已停用</span>'}</h3>
                <p class="desc">${escapeHtml(v.description || '暂无描述')}</p>
                <div class="meta">
                    <span>🕐 ${v.open_time}-${v.close_time}</span>
                    <span>👥 ${v.capacity}人</span>
                    <span>📅 日限${v.daily_quota}场</span>
                </div>
                <div class="actions">
                    <button class="btn btn-sm" onclick="editVenue(${v.id})">编辑</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteVenue(${v.id})">删除</button>
                </div>
            </div>
        `).join('');
    }).catch(err => alert(err.message));
}

function openVenueModal(venue) {
    document.getElementById('venueModal').classList.add('show');
    document.getElementById('venueForm').reset();
    document.getElementById('venueId').value = '';
    document.getElementById('venueModalTitle').textContent = '新增场地';

    if (venue) {
        document.getElementById('venueModalTitle').textContent = '编辑场地';
        document.getElementById('venueId').value = venue.id;
        document.getElementById('venueName').value = venue.name;
        document.getElementById('venueDesc').value = venue.description || '';
        document.getElementById('venueCapacity').value = venue.capacity;
        document.getElementById('venueQuota').value = venue.daily_quota;
        document.getElementById('venueOpen').value = venue.open_time;
        document.getElementById('venueClose').value = venue.close_time;
        document.getElementById('venueActive').checked = venue.is_active;
    }
}

function closeVenueModal() {
    document.getElementById('venueModal').classList.remove('show');
}

function editVenue(id) {
    apiGet('/venues/' + id).then(v => openVenueModal(v)).catch(err => alert(err.message));
}

function saveVenue(e) {
    e.preventDefault();
    const id = document.getElementById('venueId').value;
    const data = {
        name: document.getElementById('venueName').value.trim(),
        description: document.getElementById('venueDesc').value,
        capacity: parseInt(document.getElementById('venueCapacity').value) || 0,
        daily_quota: parseInt(document.getElementById('venueQuota').value) || 1,
        open_time: document.getElementById('venueOpen').value,
        close_time: document.getElementById('venueClose').value,
        is_active: document.getElementById('venueActive').checked,
        operator: getOperator()
    };

    const promise = id ? apiPut('/venues/' + id, data) : apiPost('/venues', data);
    promise.then(() => {
        closeVenueModal();
        loadVenues();
    }).catch(err => alert(err.message));
}

function deleteVenue(id) {
    if (!confirm('确定要删除/停用这个场地吗？')) return;
    apiDelete('/venues/' + id + '?operator=' + encodeURIComponent(getOperator()))
        .then(() => { loadVenues(); })
        .catch(err => alert(err.message));
}

function loadApplications() {
    const status = document.getElementById('filterStatus').value;
    let url = '/applications';
    const params = [];
    if (status) params.push('status=' + encodeURIComponent(status));
    const op = getOperator();
    if (op) params.push('viewer=' + encodeURIComponent(op));
    if (params.length) url += '?' + params.join('&');

    apiGet(url).then(apps => {
        const container = document.getElementById('applicationList');
        if (apps.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无申请记录</div>';
            return;
        }
        container.innerHTML = apps.map(a => renderAppItem(a)).join('');
    }).catch(err => alert(err.message));
}

function renderPrecheckBadge(a) {
    if (!a.precheck) return '';
    const info = PRECHECK_LABEL[a.precheck.expected_result];
    if (!info || !info.text) return '';
    return `<span class="precheck-badge ${info.cls}">${info.text}</span>`;
}

function renderAppItem(a) {
    return `
        <div class="app-item">
            <div class="info">
                <div class="title-row">
                    <h4>${escapeHtml(a.event_name)}</h4>
                    <span class="status-badge status-${a.status}">${STATUS_MAP[a.status] || a.status}</span>
                    ${renderPrecheckBadge(a)}
                </div>
                <div class="subtitle">
                    <span>🏢 ${escapeHtml(a.venue_name)}</span>
                    <span>📅 ${a.apply_date}</span>
                    <span>🕐 ${a.start_time}-${a.end_time}</span>
                    <span>👤 ${escapeHtml(a.applicant_name)}</span>
                </div>
            </div>
            <div class="actions">
                <button class="btn btn-sm" onclick="showAppDetail(${a.id})">详情</button>
                ${renderAppActions(a)}
            </div>
        </div>
    `;
}

function renderAppActions(a) {
    let html = '';
    const op = getOperator();
    const isOwner = a.applicant_name && a.applicant_name.trim() === op.trim();
    if (CURRENT_IS_APPROVER && (a.status === 'pending_approval' || a.status === 'submitted')) {
        html += `<button class="btn btn-sm btn-success" onclick="approveApp(${a.id})">通过</button>`;
        html += `<button class="btn btn-sm btn-danger" onclick="rejectApp(${a.id})">驳回</button>`;
    }
    if ((CURRENT_IS_APPROVER || isOwner) && (a.status === 'confirmed' || a.status === 'pending_approval' || a.status === 'submitted')) {
        html += `<button class="btn btn-sm btn-warning" onclick="cancelApp(${a.id})">取消</button>`;
    }
    if (CURRENT_IS_APPROVER && a.status === 'cancelled') {
        html += `<button class="btn btn-sm" onclick="revokeApp(${a.id})">撤销取消</button>`;
    }
    return html;
}

function openApplicationModal() {
    document.getElementById('applicationModal').classList.add('show');
    document.getElementById('applicationForm').reset();
    document.getElementById('appFormError').classList.remove('show');

    const today = new Date().toISOString().split('T')[0];
    document.getElementById('appDate').value = today;
    document.getElementById('appStart').value = '10:00';
    document.getElementById('appEnd').value = '12:00';

    apiGet('/venues').then(venues => {
        const activeVenues = venues.filter(v => v.is_active);
        const sel = document.getElementById('appVenueId');
        sel.innerHTML = activeVenues.map(v => `<option value="${v.id}">${escapeHtml(v.name)} (${v.open_time}-${v.close_time})</option>`).join('');
    });
}

function closeApplicationModal() {
    document.getElementById('applicationModal').classList.remove('show');
}

function saveApplication(e) {
    e.preventDefault();
    const errBox = document.getElementById('appFormError');
    errBox.classList.remove('show');

    const data = {
        venue_id: parseInt(document.getElementById('appVenueId').value),
        event_name: document.getElementById('appEventName').value.trim(),
        applicant_name: document.getElementById('appApplicant').value.trim(),
        applicant_phone: document.getElementById('appPhone').value,
        event_description: document.getElementById('appDesc').value,
        participants: parseInt(document.getElementById('appParticipants').value) || 0,
        apply_date: document.getElementById('appDate').value,
        start_time: document.getElementById('appStart').value,
        end_time: document.getElementById('appEnd').value,
        created_by: getOperator()
    };

    apiPost('/applications', data).then(() => {
        closeApplicationModal();
        loadApplications();
    }).catch(err => {
        errBox.textContent = err.message;
        errBox.classList.add('show');
    });
}

function showAppDetail(id) {
    const op = getOperator();
    const url = '/applications/' + id + (op ? '?viewer=' + encodeURIComponent(op) : '');
    apiGet(url).then(app => {
        const content = document.getElementById('detailContent');
        content.innerHTML = `
            <div class="detail-section">
                <h4>基本信息</h4>
                <div class="detail-grid">
                    <div class="label">活动名称</div><div class="value">${escapeHtml(app.event_name)}</div>
                    <div class="label">场地</div><div class="value">${escapeHtml(app.venue_name)}</div>
                    <div class="label">日期</div><div class="value">${app.apply_date}</div>
                    <div class="label">时间</div><div class="value">${app.start_time} - ${app.end_time}</div>
                    <div class="label">申请人</div><div class="value">${escapeHtml(app.applicant_name)}</div>
                    <div class="label">联系电话</div><div class="value">${escapeHtml(app.applicant_phone || '-')}</div>
                    <div class="label">参与人数</div><div class="value">${app.participants} 人</div>
                    <div class="label">当前状态</div><div class="value"><span class="status-badge status-${app.status}">${STATUS_MAP[app.status]}</span></div>
                    <div class="label">活动描述</div><div class="value">${escapeHtml(app.event_description || '-')}</div>
                </div>
            </div>

            ${renderPrecheckPanel(app)}

            <div class="detail-section">
                <h4>审批信息</h4>
                <div class="detail-grid">
                    <div class="label">审批人</div><div class="value">${escapeHtml(app.approved_by || '-')}</div>
                    <div class="label">审批时间</div><div class="value">${app.approved_at ? new Date(app.approved_at).toLocaleString('zh-CN') : '-'}</div>
                    <div class="label">审批意见</div><div class="value">${escapeHtml(app.approval_comment || '-')}</div>
                    <div class="label">审批结论</div><div class="value">${escapeHtml(app.approval_conclusion || '-')}</div>
                    <div class="label">冲突摘要</div><div class="value">${escapeHtml(app.conflict_summary || '-')}</div>
                    <div class="label">最近预检</div><div class="value">${app.last_precheck_at ? new Date(app.last_precheck_at).toLocaleString('zh-CN') + '（' + escapeHtml(app.last_precheck_by || '-') + '）' : '-'}</div>
                    <div class="label">取消原因</div><div class="value">${escapeHtml(app.cancel_reason || '-')}</div>
                    <div class="label">取消人</div><div class="value">${escapeHtml(app.cancelled_by || '-')}</div>
                </div>
            </div>

            <div class="detail-section">
                <h4>状态历史</h4>
                <div class="history-timeline">
                    ${app.status_history.map(h => `
                        <div class="history-item">
                            <div class="h-time">${new Date(h.created_at).toLocaleString('zh-CN')}</div>
                            <div class="h-content">
                                <div class="h-action">${ACTION_MAP[h.action] || h.action}：${h.from_status ? STATUS_MAP[h.from_status] + ' → ' : ''}${STATUS_MAP[h.to_status]}</div>
                                <div class="h-meta">操作人：${escapeHtml(h.operator || '系统')}</div>
                                ${h.comment ? '<div class="h-comment">' + escapeHtml(h.comment) + '</div>' : ''}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>

            <div class="detail-section">
                <div class="detail-actions">
                    ${renderDetailActions(app)}
                </div>
            </div>
        `;
        document.getElementById('detailModal').classList.add('show');
    }).catch(err => alert(err.message));
}

function renderDetailActions(app) {
    let html = '';
    const op = getOperator();
    const isOwner = app.applicant_name && app.applicant_name.trim() === op.trim();
    if (CURRENT_IS_APPROVER && (app.status === 'pending_approval' || app.status === 'submitted')) {
        html += `<button class="btn btn-success" onclick="approveApp(${app.id}); closeDetailModal();">审批通过</button>`;
        html += `<button class="btn btn-danger" onclick="rejectApp(${app.id}); closeDetailModal();">审批驳回</button>`;
    }
    if ((CURRENT_IS_APPROVER || isOwner) && ['confirmed', 'pending_approval', 'submitted'].includes(app.status)) {
        html += `<button class="btn btn-warning" onclick="cancelApp(${app.id}); closeDetailModal();">取消申请</button>`;
    }
    if (CURRENT_IS_APPROVER && app.status === 'cancelled') {
        html += `<button class="btn btn-primary" onclick="revokeApp(${app.id}); closeDetailModal();">撤销取消</button>`;
    }
    return html;
}

function closeDetailModal() {
    document.getElementById('detailModal').classList.remove('show');
}

function approveApp(id) {
    const comment = prompt('请输入审批意见（可选）：', '');
    if (comment === null) return;
    apiPost('/applications/' + id + '/approve', {
        operator: getOperator(),
        comment: comment
    }).then(() => {
        loadApplications();
        loadApprovalList();
        loadSchedule();
    }).catch(err => alert('审批失败：' + err.message));
}

function rejectApp(id) {
    const reason = prompt('请输入驳回原因：', '');
    if (reason === null || !reason.trim()) {
        alert('请填写驳回原因');
        return;
    }
    apiPost('/applications/' + id + '/reject', {
        operator: getOperator(),
        reason: reason.trim()
    }).then(() => {
        loadApplications();
        loadApprovalList();
        loadSchedule();
    }).catch(err => alert('驳回失败：' + err.message));
}

function cancelApp(id) {
    const reason = prompt('请输入取消原因（可选）：', '');
    if (reason === null) return;
    apiPost('/applications/' + id + '/cancel', {
        operator: getOperator(),
        reason: reason
    }).then(() => {
        loadApplications();
        loadApprovalList();
        loadSchedule();
    }).catch(err => alert('取消失败：' + err.message));
}

function revokeApp(id) {
    if (!confirm('确定要撤销取消，恢复到之前的状态吗？')) return;
    apiPost('/applications/' + id + '/revoke', {
        operator: getOperator()
    }).then(() => {
        loadApplications();
        loadApprovalList();
        loadSchedule();
    }).catch(err => alert('撤销失败：' + err.message));
}

function loadApprovalList() {
    if (!CURRENT_IS_APPROVER) {
        const container = document.getElementById('approvalList');
        if (container) container.innerHTML = '<div class="empty-state">无权访问，需审批人权限</div>';
        return;
    }
    const op = getOperator();
    const url = '/applications?status=pending_approval' + (op ? '&viewer=' + encodeURIComponent(op) : '');
    apiGet(url).then(apps => {
        const container = document.getElementById('approvalList');
        if (apps.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无待审批申请</div>';
            return;
        }
        container.innerHTML = apps.map(a => renderAppItem(a)).join('');
    }).catch(err => alert(err.message));
}

function renderConflictLinks(list) {
    if (!list || list.length === 0) return '<span style="color:#9ca3af;">无</span>';
    return list.map(c => `
        <a href="javascript:void(0)" class="conflict-link" onclick="showAppDetail(${c.id})">
            #${c.id} ${escapeHtml(c.event_name)} (${c.start_time}-${c.end_time} · ${STATUS_MAP[c.status] || c.status})
        </a>
    `).join('<br>');
}

function renderPrecheckPanel(app) {
    if (!app.precheck) return '';
    const p = app.precheck;
    const info = PRECHECK_LABEL[p.expected_result] || { text: '预检：未知', cls: '' };
    const resultBadge = info.text ? `<span class="precheck-badge precheck-badge-lg ${info.cls}">${info.text}</span>` : '';

    let warningNote = '';
    if (p.expected_result !== 'pass' && p.expected_result !== 'not_applicable') {
        warningNote = '<div class="precheck-note">⚠️ 预检仅为参考，正式审批会再次实时校验，结果以正式审批为准。</div>';
    } else if (p.expected_result === 'pass') {
        warningNote = '<div class="precheck-note precheck-note-ok">✅ 预检无冲突、配额充足，正式审批仍会实时复核。</div>';
    }

    return `
        <div class="detail-section">
            <h4>审批前预检 ${resultBadge}</h4>
            <div class="detail-grid">
                <div class="label">场地</div><div class="value">${escapeHtml(p.venue_name)}</div>
                <div class="label">日期</div><div class="value">${p.apply_date}</div>
                <div class="label">当日已确认</div><div class="value">${p.confirmed_count} / ${p.daily_quota} 场（剩余 ${p.quota_remaining} 场）</div>
            </div>
            <div class="precheck-block">
                <div class="precheck-block-title">同场地已确认占用</div>
                <div class="precheck-block-body">
                    ${p.confirmed_same_day && p.confirmed_same_day.length
                        ? p.confirmed_same_day.map(c => `
                            <div class="precheck-row ${p.confirmed_conflicts && p.confirmed_conflicts.some(x => x.id === c.id) ? 'precheck-conflict-row' : ''}">
                                <span class="precheck-time">${c.start_time}-${c.end_time}</span>
                                <a href="javascript:void(0)" class="conflict-link" onclick="showAppDetail(${c.id})">
                                    #${c.id} ${escapeHtml(c.event_name)}（${escapeHtml(c.applicant_name)}）
                                </a>
                                ${p.confirmed_conflicts && p.confirmed_conflicts.some(x => x.id === c.id)
                                    ? '<span class="precheck-tag precheck-tag-danger">冲突</span>' : ''}
                            </div>
                        `).join('')
                        : '<span style="color:#9ca3af;">当日暂无已确认占用</span>'}
                </div>
            </div>
            <div class="precheck-block">
                <div class="precheck-block-title">待审批重叠项</div>
                <div class="precheck-block-body">
                    ${p.pending_same_day && p.pending_same_day.length
                        ? p.pending_same_day.map(c => `
                            <div class="precheck-row ${p.pending_conflicts && p.pending_conflicts.some(x => x.id === c.id) ? 'precheck-conflict-row' : ''}">
                                <span class="precheck-time">${c.start_time}-${c.end_time}</span>
                                <a href="javascript:void(0)" class="conflict-link" onclick="showAppDetail(${c.id})">
                                    #${c.id} ${escapeHtml(c.event_name)}（${escapeHtml(c.applicant_name)}）
                                </a>
                                ${p.pending_conflicts && p.pending_conflicts.some(x => x.id === c.id)
                                    ? '<span class="precheck-tag precheck-tag-warning">重叠</span>' : ''}
                            </div>
                        `).join('')
                        : '<span style="color:#9ca3af;">当日暂无其他待审批申请</span>'}
                </div>
            </div>
            ${warningNote}
        </div>
    `;
}

function loadSchedule() {
    let date = document.getElementById('scheduleDate').value;
    if (!date) {
        date = new Date().toISOString().split('T')[0];
        document.getElementById('scheduleDate').value = date;
    }

    apiGet('/schedule/' + date).then(data => {
        const container = document.getElementById('scheduleView');
        if (data.venues.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无可用场地</div>';
            return;
        }
        container.innerHTML = data.venues.map(v => `
            <div class="schedule-venue">
                <div class="schedule-venue-header">
                    <h3>${escapeHtml(v.venue.name)}</h3>
                    <div class="quota">今日已排 ${v.confirmed_count} / ${v.daily_quota} 场 · 营业时间 ${v.venue.open_time}-${v.venue.close_time}</div>
                </div>
                <div class="schedule-timeline">
                    ${v.applications.length === 0
                        ? '<div style="color:#9ca3af;font-size:13px;">当日暂无已确认排期</div>'
                        : v.applications.map(a => `
                            <div class="schedule-item">
                                <div class="time">${a.start_time} - ${a.end_time}</div>
                                <div class="event">${escapeHtml(a.event_name)}</div>
                                <div class="applicant">${escapeHtml(a.applicant_name)} · ${a.participants}人</div>
                            </div>
                        `).join('')
                    }
                </div>
            </div>
        `).join('');
    }).catch(err => alert(err.message));
}

function exportSchedule() {
    const date = document.getElementById('scheduleDate').value;
    if (!date) return;
    const operator = encodeURIComponent(getOperator());
    window.location.href = API + '/schedule/' + date + '/export?operator=' + operator;
}

function loadAuditLogs() {
    apiGet('/audit-logs?limit=200').then(logs => {
        const container = document.getElementById('auditLogList');
        if (logs.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无操作日志</div>';
            return;
        }
        container.innerHTML = logs.map(l => `
            <div class="log-item">
                <span class="log-time">${new Date(l.created_at).toLocaleString('zh-CN')}</span>
                <span class="log-actor">${escapeHtml(l.actor || '匿名')}</span>
                <span class="log-action">${escapeHtml(l.action)}</span>
                <span class="log-detail">${escapeHtml(l.detail)}</span>
            </div>
        `).join('');
    }).catch(err => alert(err.message));
}

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

document.addEventListener('DOMContentLoaded', () => {
    refreshRole().then(() => {
        loadVenues();
        loadApplications();
    });
});

window.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('show');
    }
});
