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
        const importReviewBtns = document.querySelectorAll('.tab-btn[data-tab="importReview"]');
        const importReviewPanel = document.getElementById('tab-importReview');
        const closureBtns = document.querySelectorAll('.tab-btn[data-tab="closures"]');
        const closurePanel = document.getElementById('tab-closures');
        if (CURRENT_IS_APPROVER) {
            badge.textContent = '审批人';
            badge.className = 'status-badge status-confirmed';
            hint.textContent = '';
            approvalBtns.forEach(b => b.style.display = '');
            if (approvalPanel) approvalPanel.style.display = '';
            importReviewBtns.forEach(b => b.style.display = '');
            if (importReviewPanel) importReviewPanel.style.display = '';
            closureBtns.forEach(b => b.style.display = '');
            if (closurePanel) closurePanel.style.display = '';
        } else {
            badge.textContent = '普通申请人';
            badge.className = 'status-badge role-badge-applicant';
            hint.textContent = '（审批人: ' + APPROVER_LIST.join('、') + '）';
            approvalBtns.forEach(b => b.style.display = 'none');
            importReviewBtns.forEach(b => b.style.display = 'none');
            closureBtns.forEach(b => b.style.display = 'none');
            const activeApprovalBtn = document.querySelector('.tab-btn.active[data-tab="approval"]');
            const activeImportReviewBtn = document.querySelector('.tab-btn.active[data-tab="importReview"]');
            const activeClosureBtn = document.querySelector('.tab-btn.active[data-tab="closures"]');
            if (activeApprovalBtn || activeImportReviewBtn || activeClosureBtn ||
                (approvalPanel && approvalPanel.classList.contains('active')) ||
                (importReviewPanel && importReviewPanel.classList.contains('active')) ||
                (closurePanel && closurePanel.classList.contains('active'))) {
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
            if (importReviewPanel) importReviewPanel.style.display = 'none';
            if (closurePanel) closurePanel.style.display = 'none';
        }

        const activeTab = document.querySelector('.tab-btn.active');
        if (activeTab) {
            const tab = activeTab.dataset.tab;
            if (tab === 'venues') loadVenues();
            if (tab === 'applications') loadApplications();
            if (tab === 'approval') loadApprovalList();
            if (tab === 'importReview') loadImportBatches();
            if (tab === 'closures') loadClosures();
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
        if (tab === 'importReview') loadImportBatches();
        if (tab === 'closures') loadClosures();
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

const IMPORT_STATUS_LABEL = {
    'preview': '预演中',
    'confirmed': '已确认待导入',
    'completed': '已完成',
    'cancelled': '已取消',
};

const IMPORT_RECORD_STATUS_LABEL = {
    'pending': '待处理',
    'preview_pass': '预演通过',
    'preview_fail': '预演失败',
    'import_success': '导入成功',
    'import_fail': '导入失败',
    'duplicate_in_batch': '批内重复',
};

const ERROR_CATEGORY_LABEL_JS = {
    'venue_not_found': '场地不存在',
    'venue_inactive': '场地已停用',
    'invalid_hours': '营业时间不合法',
    'time_conflict': '时段冲突',
    'quota_exceeded': '日配额超限',
    'duplicate_history': '历史重复',
    'duplicate_in_batch': '批内重复',
    'validation_error': '校验错误',
    'system_error': '系统异常',
};

function loadImportBatches() {
    if (!CURRENT_IS_APPROVER) {
        document.getElementById('importBatchList').innerHTML = '<div class="empty-state">无权访问，需审批人权限</div>';
        return;
    }
    const batchStatus = document.getElementById('importBatchStatusFilter').value;
    const approvalStatus = document.getElementById('importApprovalStatusFilter').value;
    const importResult = document.getElementById('importResultFilter').value;
    const op = encodeURIComponent(getOperator());
    let url = '/import?operator=' + op;
    if (batchStatus) url += '&batch_status=' + encodeURIComponent(batchStatus);
    if (approvalStatus) url += '&approval_status=' + encodeURIComponent(approvalStatus);
    if (importResult) url += '&import_result=' + encodeURIComponent(importResult);

    apiGet(url).then(batches => {
        const container = document.getElementById('importBatchList');
        if (batches.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无导入批次</div>';
            return;
        }
        container.innerHTML = batches.map(b => renderImportBatchItem(b)).join('');
    }).catch(err => alert(err.message));
}

function renderImportBatchItem(b) {
    const eb = b.error_breakdown || {};
    const ab = b.approval_breakdown || {};
    const ebParts = [];
    if (eb.venue_not_found) ebParts.push(`场地不存在${eb.venue_not_found}`);
    if (eb.venue_inactive) ebParts.push(`已停用${eb.venue_inactive}`);
    if (eb.invalid_hours) ebParts.push(`营业时间${eb.invalid_hours}`);
    if (eb.time_conflict) ebParts.push(`时段冲突${eb.time_conflict}`);
    if (eb.quota_exceeded) ebParts.push(`配额超限${eb.quota_exceeded}`);
    if (eb.duplicate_in_batch) ebParts.push(`批内重复${eb.duplicate_in_batch}`);
    if (eb.duplicate_history) ebParts.push(`历史重复${eb.duplicate_history}`);
    if (eb.validation_error) ebParts.push(`校验错误${eb.validation_error}`);
    if (eb.system_error) ebParts.push(`系统异常${eb.system_error}`);

    const abParts = [];
    if (ab.pending_approval) abParts.push(`待审批${ab.pending_approval}`);
    if (ab.confirmed) abParts.push(`已确认${ab.confirmed}`);
    if (ab.cancelled) abParts.push(`已取消${ab.cancelled}`);
    if (ab.rejected) abParts.push(`已驳回${ab.rejected}`);
    if (ab.submitted) abParts.push(`已提交${ab.submitted}`);

    return `
        <div class="import-batch-item">
            <div class="info">
                <div class="title-row">
                    <h4>#${b.id} ${escapeHtml(b.filename)}</h4>
                    <span class="status-badge status-${b.status}">${IMPORT_STATUS_LABEL[b.status] || b.status}</span>
                </div>
                <div class="subtitle">
                    <span>👤 导入人：${escapeHtml(b.created_by)}</span>
                    <span>📅 创建：${new Date(b.created_at).toLocaleString('zh-CN')}</span>
                    ${b.confirmed_by ? `<span>✅ 确认人：${escapeHtml(b.confirmed_by)}</span>` : ''}
                </div>
                <div class="import-stats">
                    <span class="import-stat-item"><span class="label">总计</span><span class="value">${b.total_count}</span></span>
                    <span class="import-stat-item"><span class="label">成功</span><span class="value success">${b.success_count}</span></span>
                    <span class="import-stat-item"><span class="label">失败</span><span class="value fail">${b.failed_count}</span></span>
                    ${abParts.length ? `<span class="import-stat-item"><span class="label">审批</span><span class="value warning">${abParts.join(' / ')}</span></span>` : ''}
                    ${ebParts.length ? `<span class="import-stat-item"><span class="label">失败原因</span><span class="value fail">${ebParts.join(' / ')}</span></span>` : ''}
                </div>
            </div>
            <div class="actions">
                <button class="btn btn-sm" onclick="showImportBatchDetail(${b.id})">查看详情</button>
                <button class="btn btn-sm btn-success" onclick="exportImportBatch(${b.id})">导出CSV</button>
            </div>
        </div>
    `;
}

function showImportBatchDetail(batchId) {
    const op = encodeURIComponent(getOperator());
    apiGet('/import/' + batchId + '?operator=' + op).then(batch => {
        document.getElementById('importBatchDetailTitle').textContent = `批次 #${batch.id} - ${batch.filename}`;
        renderImportBatchDetailContent(batch);
        document.getElementById('importBatchDetailModal').classList.add('show');
    }).catch(err => alert(err.message));
}

function closeImportBatchDetailModal() {
    document.getElementById('importBatchDetailModal').classList.remove('show');
}

function renderImportBatchDetailContent(batch) {
    const eb = batch.error_breakdown || {};
    const ab = batch.approval_breakdown || {};
    const records = batch.records || [];

    let recordsHtml = '';
    if (records.length === 0) {
        recordsHtml = '<div class="empty-state" style="padding:20px;">暂无记录</div>';
    } else {
        recordsHtml = records.map(r => renderImportRecordItem(batch.id, r)).join('');
    }

    const logs = batch.related_audit_logs || [];
    const appLogs = batch.related_application_logs || [];
    const allLogs = [...logs, ...appLogs].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    const content = `
        <div class="detail-section">
            <div class="batch-summary-card">
                <h4>批次摘要</h4>
                <div class="batch-summary-grid">
                    <div class="batch-summary-cell"><div class="k">总记录</div><div class="v">${batch.total_count}</div></div>
                    <div class="batch-summary-cell"><div class="k">导入成功</div><div class="v" style="color:#059669;">${batch.success_count}</div></div>
                    <div class="batch-summary-cell"><div class="k">导入失败</div><div class="v" style="color:#dc2626;">${batch.failed_count}</div></div>
                    <div class="batch-summary-cell"><div class="k">待审批</div><div class="v">${ab.pending_approval || 0}</div></div>
                    <div class="batch-summary-cell"><div class="k">已确认</div><div class="v" style="color:#059669;">${ab.confirmed || 0}</div></div>
                    <div class="batch-summary-cell"><div class="k">已取消</div><div class="v" style="color:#6b7280;">${ab.cancelled || 0}</div></div>
                    <div class="batch-summary-cell"><div class="k">已驳回</div><div class="v" style="color:#dc2626;">${ab.rejected || 0}</div></div>
                    <div class="batch-summary-cell"><div class="k">导入人</div><div class="v" style="font-size:13px;">${escapeHtml(batch.created_by)}</div></div>
                </div>
            </div>

            <div class="detail-grid">
                <div class="label">文件名</div><div class="value">${escapeHtml(batch.filename)}</div>
                <div class="label">批次状态</div><div class="value"><span class="status-badge status-${batch.status}">${IMPORT_STATUS_LABEL[batch.status] || batch.status}</span></div>
                <div class="label">创建时间</div><div class="value">${new Date(batch.created_at).toLocaleString('zh-CN')}</div>
                ${batch.confirmed_by ? `<div class="label">确认人/时间</div><div class="value">${escapeHtml(batch.confirmed_by)} / ${batch.confirmed_at ? new Date(batch.confirmed_at).toLocaleString('zh-CN') : '-'}</div>` : ''}
                <div class="label">预演摘要</div><div class="value">${escapeHtml(batch.preview_summary || '-')}</div>
                ${batch.failure_summary ? `<div class="label">失败摘要</div><div class="value" style="color:#dc2626;">${escapeHtml(batch.failure_summary)}</div>` : ''}
            </div>
        </div>

        <div class="detail-section">
            <h4>导入记录明细（${records.length} 条）</h4>
            <div class="import-detail-filter-bar">
                <label style="font-size:13px;color:#6b7280;">筛选：</label>
                <select id="detailRecordStatusFilter" onchange="filterImportBatchRecords(${batch.id})">
                    <option value="">全部导入状态</option>
                    <option value="import_success">仅导入成功</option>
                    <option value="import_fail">仅导入失败</option>
                    <option value="duplicate_in_batch">仅批内重复</option>
                    <option value="preview_pass">仅预演通过</option>
                    <option value="preview_fail">仅预演失败</option>
                </select>
                <select id="detailErrorCategoryFilter" onchange="filterImportBatchRecords(${batch.id})">
                    <option value="">全部失败分类</option>
                    <option value="venue_not_found">场地不存在</option>
                    <option value="venue_inactive">场地已停用</option>
                    <option value="invalid_hours">营业时间不合法</option>
                    <option value="time_conflict">时段冲突</option>
                    <option value="quota_exceeded">日配额超限</option>
                    <option value="duplicate_in_batch">批内重复</option>
                    <option value="duplicate_history">历史重复</option>
                    <option value="validation_error">校验错误</option>
                    <option value="system_error">系统异常</option>
                </select>
                <select id="detailApprovalStatusFilter" onchange="filterImportBatchRecords(${batch.id})">
                    <option value="">全部审批状态</option>
                    <option value="pending_approval">待审批</option>
                    <option value="confirmed">已确认</option>
                    <option value="cancelled">已取消</option>
                    <option value="rejected">已驳回</option>
                    <option value="submitted">已提交</option>
                </select>
                <button class="btn btn-sm btn-success" onclick="exportImportBatch(${batch.id})">📥 导出本批次</button>
            </div>
            <div id="importRecordsContainer">
                ${recordsHtml}
            </div>
        </div>

        <div class="detail-section">
            <h4>相关操作日志（${allLogs.length} 条）</h4>
            <div class="audit-log-section">
                ${allLogs.length === 0 ? '<div class="empty-state" style="padding:20px;">暂无日志</div>' :
                    allLogs.slice(0, 50).map(l => `
                        <div class="log-item">
                            <span class="log-time">${new Date(l.created_at).toLocaleString('zh-CN')}</span>
                            <span class="log-actor">${escapeHtml(l.actor || '匿名')}</span>
                            <span class="log-action">${escapeHtml(l.action)}</span>
                            <span class="log-detail">${escapeHtml(l.detail || '')}</span>
                        </div>
                    `).join('')
                }
            </div>
        </div>
    `;
    document.getElementById('importBatchDetailContent').innerHTML = content;
}

function filterImportBatchRecords(batchId) {
    const recordStatus = document.getElementById('detailRecordStatusFilter').value;
    const errorCategory = document.getElementById('detailErrorCategoryFilter').value;
    const approvalStatus = document.getElementById('detailApprovalStatusFilter').value;
    const op = encodeURIComponent(getOperator());
    let url = '/import/' + batchId + '?operator=' + op;
    if (recordStatus) url += '&record_status=' + encodeURIComponent(recordStatus);
    if (errorCategory) url += '&error_category=' + encodeURIComponent(errorCategory);
    if (approvalStatus) url += '&approval_status=' + encodeURIComponent(approvalStatus);

    apiGet(url).then(batch => {
        const records = batch.records || [];
        const container = document.getElementById('importRecordsContainer');
        if (records.length === 0) {
            container.innerHTML = '<div class="empty-state" style="padding:20px;">筛选后无匹配记录</div>';
        } else {
            container.innerHTML = records.map(r => renderImportRecordItem(batchId, r)).join('');
        }
    }).catch(err => alert(err.message));
}

function renderImportRecordItem(batchId, r) {
    let itemClass = '';
    if (r.status === 'import_success' || r.status === 'preview_pass') itemClass = 'success';
    else if (r.status === 'duplicate_in_batch') itemClass = 'duplicate';
    else itemClass = 'fail';

    const appInfo = r.application || null;
    const conflictApp = r.conflict_application || null;

    return `
        <div class="import-record-item ${itemClass}">
            <div class="import-record-header">
                <div class="import-record-title">
                    <span>第${r.line_number}行</span>
                    <span class="status-badge status-${r.status}">${IMPORT_RECORD_STATUS_LABEL[r.status] || r.status}</span>
                    ${r.error_category ? `<span class="error-category-badge ${r.error_category}">${ERROR_CATEGORY_LABEL_JS[r.error_category] || r.error_category}</span>` : ''}
                    <span style="font-weight:500;">${escapeHtml(r.event_name)}</span>
                </div>
                <div class="import-record-actions">
                    ${r.application_id ? `<button class="btn btn-sm btn-primary" onclick="showAppDetail(${r.application_id}); closeImportBatchDetailModal();">查看申请</button>` : ''}
                    ${r.conflict_with_application_id ? `<button class="btn btn-sm btn-warning" onclick="showAppDetail(${r.conflict_with_application_id}); closeImportBatchDetailModal();">查看冲突申请</button>` : ''}
                    ${r.application_id ? `<button class="btn btn-sm" onclick="showRecordLogs(${batchId}, ${r.id})">操作日志</button>` : ''}
                </div>
            </div>
            <div class="import-record-meta">
                <span>🏢 ${escapeHtml(r.venue_name)}${r.venue_id ? `(#${r.venue_id})` : ''}</span>
                <span>👤 ${escapeHtml(r.applicant_name)}</span>
                <span>📅 ${r.apply_date || '-'}</span>
                <span>🕐 ${r.start_time || '-'} - ${r.end_time || '-'}</span>
                <span>👥 ${r.participants}人</span>
            </div>
            ${appInfo ? `
                <div class="import-record-app-info">
                    <span>📋 关联申请 #${r.application_id}</span>
                    <span class="status-badge status-${appInfo.status}">${appInfo.status_label || appInfo.status}</span>
                    ${appInfo.approved_by ? `<span>✅ 审批人：${escapeHtml(appInfo.approved_by)}</span>` : ''}
                    ${appInfo.approval_conclusion ? `<span>📝 ${escapeHtml(appInfo.approval_conclusion)}</span>` : ''}
                    ${appInfo.cancelled_by ? `<span>❌ 取消人：${escapeHtml(appInfo.cancelled_by)}</span>` : ''}
                </div>
            ` : ''}
            ${conflictApp ? `
                <div class="import-record-error" style="background:#fff7ed;color:#92400e;">
                    ⚠️ 与冲突申请 #${conflictApp.id}「${escapeHtml(conflictApp.event_name)}」重叠：${conflictApp.apply_date || ''} ${conflictApp.start_time || ''}-${conflictApp.end_time || ''}（${conflictApp.status_label || conflictApp.status}）
                </div>
            ` : ''}
            ${r.error_message ? `<div class="import-record-error">❌ ${escapeHtml(r.error_message)}</div>` : ''}
        </div>
    `;
}

function showRecordLogs(batchId, recordId) {
    const op = encodeURIComponent(getOperator());
    apiGet(`/import/${batchId}/records/${recordId}/logs?operator=${op}`).then(data => {
        const appLogs = data.application_logs || [];
        const statusHistory = data.status_history || [];
        const conflictApp = data.conflict_application || null;

        let html = '<div class="detail-section">';

        if (conflictApp) {
            html += `<h4>冲突申请信息</h4>`;
            html += `<div class="import-record-app-info">`;
            html += `<span>📋 #${conflictApp.id} ${escapeHtml(conflictApp.event_name)}</span>`;
            html += `<span class="status-badge status-${conflictApp.status}">${STATUS_MAP[conflictApp.status] || conflictApp.status}</span>`;
            html += `<button class="btn btn-sm btn-primary" onclick="showAppDetail(${conflictApp.id}); document.getElementById('recordLogsModal').classList.remove('show'); closeImportBatchDetailModal();">查看详情</button>`;
            html += `</div>`;
        }

        html += `<h4>状态历史（${statusHistory.length} 条）</h4>`;
        if (statusHistory.length === 0) {
            html += '<div class="empty-state" style="padding:10px;">暂无状态历史</div>';
        } else {
            html += '<div class="history-timeline">';
            html += statusHistory.map(h => `
                <div class="history-item">
                    <div class="h-time">${new Date(h.created_at).toLocaleString('zh-CN')}</div>
                    <div class="h-content">
                        <div class="h-action">${ACTION_MAP[h.action] || h.action}：${h.from_status ? STATUS_MAP[h.from_status] + ' → ' : ''}${STATUS_MAP[h.to_status]}</div>
                        <div class="h-meta">操作人：${escapeHtml(h.operator || '系统')}</div>
                        ${h.comment ? '<div class="h-comment">' + escapeHtml(h.comment) + '</div>' : ''}
                    </div>
                </div>
            `).join('');
            html += '</div>';
        }

        html += `<h4 style="margin-top:16px;">操作日志（${appLogs.length} 条）</h4>`;
        if (appLogs.length === 0) {
            html += '<div class="empty-state" style="padding:10px;">暂无操作日志</div>';
        } else {
            html += '<div class="audit-log-section">';
            html += appLogs.map(l => `
                <div class="log-item">
                    <span class="log-time">${new Date(l.created_at).toLocaleString('zh-CN')}</span>
                    <span class="log-actor">${escapeHtml(l.actor || '匿名')}</span>
                    <span class="log-action">${escapeHtml(l.action)}</span>
                    <span class="log-detail">${escapeHtml(l.detail || '')}</span>
                </div>
            `).join('');
            html += '</div>';
        }

        html += '</div>';

        let modal = document.getElementById('recordLogsModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'recordLogsModal';
            modal.className = 'modal';
            modal.innerHTML = `
                <div class="modal-content modal-large">
                    <div class="modal-header">
                        <h3>记录操作日志</h3>
                        <button class="close-btn" onclick="document.getElementById('recordLogsModal').classList.remove('show');">&times;</button>
                    </div>
                    <div id="recordLogsContent"></div>
                </div>
            `;
            document.body.appendChild(modal);
            modal.addEventListener('click', (e) => {
                if (e.target.classList.contains('modal')) e.target.classList.remove('show');
            });
        }
        document.getElementById('recordLogsContent').innerHTML = html;
        modal.classList.add('show');
    }).catch(err => alert(err.message));
}

function exportImportBatch(batchId) {
    const operator = encodeURIComponent(getOperator());
    window.location.href = API + '/import/' + batchId + '/export?operator=' + operator;
}

document.addEventListener('DOMContentLoaded', () => {
    refreshRole().then(() => {
        loadVenues();
        loadApplications();
        initClosureVenueFilter();
    });
});

window.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('show');
    }
});

const CLOSURE_STATUS_LABEL = {
    'active': '生效中',
    'revoked': '已撤销'
};

function loadClosures() {
    if (!CURRENT_IS_APPROVER) return;

    const statusFilter = document.getElementById('closureStatusFilter').value;
    const venueFilter = document.getElementById('closureVenueFilter').value;
    const op = encodeURIComponent(getOperator());

    let url = '/venue-closures?viewer=' + op;
    if (statusFilter) url += '&status=' + encodeURIComponent(statusFilter);
    if (venueFilter) url += '&venue_id=' + venueFilter;

    apiGet(url).then(closures => {
        const container = document.getElementById('closureList');
        if (closures.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无封场记录</div>';
            return;
        }
        container.innerHTML = closures.map(c => renderClosureItem(c)).join('');
    }).catch(err => alert(err.message));
}

function renderClosureItem(c) {
    const timeRange = c.closure_start_time && c.closure_end_time
        ? `${c.closure_start_time}-${c.closure_end_time}`
        : '全天';
    return `
        <div class="import-batch-item">
            <div class="info">
                <div class="title-row">
                    <h4>#${c.id} ${escapeHtml(c.venue_name || '')} 封场</h4>
                    <span class="status-badge status-${c.status}">${CLOSURE_STATUS_LABEL[c.status] || c.status}</span>
                </div>
                <div class="subtitle">
                    <span>📅 ${c.closure_start_date} ~ ${c.closure_end_date}</span>
                    <span>🕐 ${timeRange}</span>
                    <span>👤 创建人：${escapeHtml(c.created_by || '-')}</span>
                </div>
                <div class="import-stats">
                    <span class="import-stat-item"><span class="label">原因</span><span class="value">${escapeHtml(c.reason || '未填写')}</span></span>
                    <span class="import-stat-item"><span class="label">影响现有</span><span class="value">${c.affects_existing_applications ? '是' : '否'}</span></span>
                    ${c.restore_note ? `<span class="import-stat-item"><span class="label">恢复备注</span><span class="value">${escapeHtml(c.restore_note)}</span></span>` : ''}
                    ${c.revoked_by ? `<span class="import-stat-item"><span class="label">撤销人</span><span class="value">${escapeHtml(c.revoked_by)}</span></span>` : ''}
                </div>
            </div>
            <div class="actions">
                <button class="btn btn-sm" onclick="showClosureDetail(${c.id})">详情</button>
                ${c.status === 'active' ? `
                    <button class="btn btn-sm btn-warning" onclick="editClosure(${c.id})">编辑</button>
                    <button class="btn btn-sm btn-danger" onclick="revokeClosure(${c.id})">撤销</button>
                ` : ''}
            </div>
        </div>
    `;
}

function openClosureModal(closure) {
    document.getElementById('closureModal').classList.add('show');
    document.getElementById('closureForm').reset();
    document.getElementById('closureId').value = '';
    document.getElementById('closureModalTitle').textContent = '新增封场';
    document.getElementById('closureAffectsExisting').checked = true;

    apiGet('/venues').then(venues => {
        const activeVenues = venues.filter(v => v.is_active);
        const sel = document.getElementById('closureVenueId');
        sel.innerHTML = activeVenues.map(v => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join('');
        if (closure) {
            document.getElementById('closureModalTitle').textContent = '编辑封场';
            document.getElementById('closureId').value = closure.id;
            document.getElementById('closureVenueId').value = closure.venue_id;
            document.getElementById('closureStartDate').value = closure.closure_start_date;
            document.getElementById('closureEndDate').value = closure.closure_end_date;
            document.getElementById('closureStartTime').value = closure.closure_start_time || '';
            document.getElementById('closureEndTime').value = closure.closure_end_time || '';
            document.getElementById('closureReason').value = closure.reason || '';
            document.getElementById('closureRestoreNote').value = closure.restore_note || '';
            document.getElementById('closureAffectsExisting').checked = closure.affects_existing_applications;
        }
    }).catch(err => alert(err.message));
}

function closeClosureModal() {
    document.getElementById('closureModal').classList.remove('show');
}

function editClosure(id) {
    const op = encodeURIComponent(getOperator());
    apiGet('/venue-closures/' + id + '?viewer=' + op).then(c => {
        openClosureModal(c);
    }).catch(err => alert(err.message));
}

function saveClosure(e) {
    e.preventDefault();
    const id = document.getElementById('closureId').value;
    const data = {
        venue_id: parseInt(document.getElementById('closureVenueId').value),
        closure_start_date: document.getElementById('closureStartDate').value,
        closure_end_date: document.getElementById('closureEndDate').value,
        closure_start_time: document.getElementById('closureStartTime').value || null,
        closure_end_time: document.getElementById('closureEndTime').value || null,
        reason: document.getElementById('closureReason').value.trim(),
        restore_note: document.getElementById('closureRestoreNote').value.trim(),
        affects_existing_applications: document.getElementById('closureAffectsExisting').checked,
        operator: getOperator()
    };

    const promise = id
        ? apiPut('/venue-closures/' + id, data)
        : apiPost('/venue-closures', data);

    promise.then(() => {
        closeClosureModal();
        loadClosures();
        loadSchedule();
    }).catch(err => alert(err.message));
}

function revokeClosure(id) {
    if (!confirm('确定要撤销这个封场吗？撤销后封场将不再生效。')) return;
    const reason = prompt('请输入撤销原因（可选）：', '');
    if (reason === null) return;

    const op = encodeURIComponent(getOperator());
    apiPost('/venue-closures/' + id + '/revoke', {
        operator: getOperator(),
        revoke_reason: reason.trim()
    }).then(() => {
        loadClosures();
        loadSchedule();
    }).catch(err => alert('撤销失败：' + err.message));
}

function showClosureDetail(id) {
    const op = encodeURIComponent(getOperator());
    apiGet('/venue-closures/' + id + '?viewer=' + op).then(closure => {
        renderClosureDetailContent(closure);
        document.getElementById('closureDetailTitle').textContent =
            `封场详情 #${closure.id} - ${closure.venue_name || ''}`;
        document.getElementById('closureDetailModal').classList.add('show');
    }).catch(err => alert(err.message));
}

function closeClosureDetailModal() {
    document.getElementById('closureDetailModal').classList.remove('show');
}

function renderClosureDetailContent(closure) {
    const timeRange = closure.closure_start_time && closure.closure_end_time
        ? `${closure.closure_start_time}-${closure.closure_end_time}`
        : '全天';
    const affectedApps = closure.affected_applications || [];
    const waivers = closure.waivers || [];
    const auditLogs = closure.audit_logs || [];

    const content = `
        <div class="detail-section">
            <h4>基本信息</h4>
            <div class="detail-grid">
                <div class="label">场地</div><div class="value">${escapeHtml(closure.venue_name || '-')}</div>
                <div class="label">状态</div><div class="value"><span class="status-badge status-${closure.status}">${CLOSURE_STATUS_LABEL[closure.status] || closure.status}</span></div>
                <div class="label">开始日期</div><div class="value">${closure.closure_start_date}</div>
                <div class="label">结束日期</div><div class="value">${closure.closure_end_date}</div>
                <div class="label">封场时段</div><div class="value">${timeRange}</div>
                <div class="label">影响现有申请</div><div class="value">${closure.affects_existing_applications ? '是' : '否'}</div>
                <div class="label">创建人</div><div class="value">${escapeHtml(closure.created_by || '-')}</div>
                <div class="label">创建时间</div><div class="value">${closure.created_at ? new Date(closure.created_at).toLocaleString('zh-CN') : '-'}</div>
                <div class="label">封场原因</div><div class="value">${escapeHtml(closure.reason || '-')}</div>
                <div class="label">恢复备注</div><div class="value">${escapeHtml(closure.restore_note || '-')}</div>
                ${closure.revoked_by ? `
                    <div class="label">撤销人</div><div class="value">${escapeHtml(closure.revoked_by)}</div>
                    <div class="label">撤销时间</div><div class="value">${closure.revoked_at ? new Date(closure.revoked_at).toLocaleString('zh-CN') : '-'}</div>
                    <div class="label">撤销原因</div><div class="value">${escapeHtml(closure.revoke_reason || '-')}</div>
                ` : ''}
            </div>
        </div>

        <div class="detail-section">
            <div class="detail-actions">
                ${closure.status === 'active' ? `
                    <button class="btn btn-warning" onclick="editClosure(${closure.id}); closeClosureDetailModal();">编辑封场</button>
                    <button class="btn btn-danger" onclick="revokeClosure(${closure.id}); closeClosureDetailModal();">撤销封场</button>
                ` : ''}
            </div>
        </div>

        <div class="detail-section">
            <h4>受影响的申请（${affectedApps.length} 个）</h4>
            ${affectedApps.length === 0
                ? '<div class="empty-state" style="padding:20px;">暂无受影响的申请</div>'
                : `<div class="list-container">
                    ${affectedApps.map(a => `
                        <div class="app-item">
                            <div class="info">
                                <div class="title-row">
                                    <h4>#${a.id} ${escapeHtml(a.event_name)}</h4>
                                    <span class="status-badge status-${a.status}">${STATUS_MAP[a.status] || a.status}</span>
                                    ${a.has_waiver ? '<span class="status-badge status-confirmed">已放行</span>' : ''}
                                </div>
                                <div class="subtitle">
                                    <span>📅 ${a.apply_date}</span>
                                    <span>🕐 ${a.start_time}-${a.end_time}</span>
                                    <span>👤 ${escapeHtml(a.applicant_name)}</span>
                                </div>
                            </div>
                            <div class="actions">
                                <button class="btn btn-sm" onclick="showAppDetail(${a.id}); closeClosureDetailModal();">查看申请</button>
                                ${closure.status === 'active' && !a.has_waiver ? `
                                    <button class="btn btn-sm btn-success" onclick="grantWaiver(${closure.id}, ${a.id})">放行</button>
                                ` : ''}
                                ${closure.status === 'active' && a.has_waiver && a.waiver ? `
                                    <button class="btn btn-sm btn-danger" onclick="revokeWaiver(${closure.id}, ${a.waiver.id})">撤销放行</button>
                                ` : ''}
                            </div>
                        </div>
                    `).join('')}
                   </div>`
            }
        </div>

        <div class="detail-section">
            <h4>放行记录（${waivers.length} 条）</h4>
            ${waivers.length === 0
                ? '<div class="empty-state" style="padding:20px;">暂无放行记录</div>'
                : `<div class="list-container">
                    ${waivers.map(w => `
                        <div class="import-record-item success">
                            <div class="import-record-header">
                                <div class="import-record-title">
                                    <span>放行 #${w.id}</span>
                                    <span class="status-badge status-confirmed">已放行</span>
                                </div>
                            </div>
                            <div class="import-record-meta">
                                <span>📋 申请 #${w.application_id}</span>
                                <span>👤 放行人：${escapeHtml(w.waived_by || '-')}</span>
                                <span>🕐 ${w.waived_at ? new Date(w.waived_at).toLocaleString('zh-CN') : '-'}</span>
                            </div>
                            ${w.waiver_reason ? `<div class="import-record-error" style="background:#ecfdf5;color:#065f46;">📝 ${escapeHtml(w.waiver_reason)}</div>` : ''}
                            ${closure.status === 'active' ? `
                                <div class="import-record-actions">
                                    <button class="btn btn-sm btn-danger" onclick="revokeWaiver(${closure.id}, ${w.id})">撤销放行</button>
                                </div>
                            ` : ''}
                        </div>
                    `).join('')}
                   </div>`
            }
        </div>

        <div class="detail-section">
            <h4>操作日志（${auditLogs.length} 条）</h4>
            <div class="audit-log-section">
                ${auditLogs.length === 0 ? '<div class="empty-state" style="padding:20px;">暂无日志</div>' :
                    auditLogs.map(l => `
                        <div class="log-item">
                            <span class="log-time">${new Date(l.created_at).toLocaleString('zh-CN')}</span>
                            <span class="log-actor">${escapeHtml(l.actor || '匿名')}</span>
                            <span class="log-action">${escapeHtml(l.action)}</span>
                            <span class="log-detail">${escapeHtml(l.detail || '')}</span>
                        </div>
                    `).join('')
                }
            </div>
        </div>
    `;
    document.getElementById('closureDetailContent').innerHTML = content;
}

function grantWaiver(closureId, applicationId) {
    const reason = prompt('请输入放行原因（可选）：', '');
    if (reason === null) return;

    apiPost('/venue-closures/' + closureId + '/waivers', {
        operator: getOperator(),
        application_id: applicationId,
        waiver_reason: reason.trim()
    }).then(() => {
        showClosureDetail(closureId);
        loadSchedule();
    }).catch(err => alert('放行失败：' + err.message));
}

function revokeWaiver(closureId, waiverId) {
    if (!confirm('确定要撤销这条放行记录吗？')) return;
    const op = encodeURIComponent(getOperator());
    apiDelete('/venue-closures/' + closureId + '/waivers/' + waiverId + '?operator=' + op)
        .then(() => {
            showClosureDetail(closureId);
            loadSchedule();
        }).catch(err => alert('撤销放行失败：' + err.message));
}

function initClosureVenueFilter() {
    apiGet('/venues').then(venues => {
        const sel = document.getElementById('closureVenueFilter');
        if (sel) {
            const activeVenues = venues.filter(v => v.is_active);
            sel.innerHTML = '<option value="">全部场地</option>' +
                activeVenues.map(v => `<option value="${v.id}">${escapeHtml(v.name)}</option>`).join('');
        }
    }).catch(() => {});
}
