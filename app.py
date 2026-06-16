import os
import json
import csv
from io import StringIO
from datetime import datetime, time, timedelta, date
from flask import Flask, request, jsonify, render_template, Response, send_file

from models import db, Venue, Application, ApplicationStatus, StatusHistory, AuditLog

app = Flask(__name__, template_folder='templates', static_folder='static')

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                    'scheduling.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_AS_ASCII'] = False

APPROVERS = {'张三', '管理员', 'admin', 'Administrator'}

db.init_app(app)

_db_initialized = False


@app.before_request
def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    with app.app_context():
        db.create_all()
        init_seed_data()
    _db_initialized = True


def is_approver(name):
    if not name:
        return False
    return name.strip() in APPROVERS


def require_approver(operator):
    if operator and operator.strip() in APPROVERS:
        return True, None
    return False, '无权执行该操作，需审批人权限'


def parse_time_str(t):
    if isinstance(t, time):
        return t
    if isinstance(t, str):
        parts = t.split(':')
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour=hour, minute=minute)
    return t


def parse_date_str(d):
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return datetime.strptime(d, '%Y-%m-%d').date()
    return d


def time_overlap(start1, end1, start2, end2):
    s1 = timedelta(hours=start1.hour, minutes=start1.minute)
    e1 = timedelta(hours=end1.hour, minutes=end1.minute)
    s2 = timedelta(hours=start2.hour, minutes=start2.minute)
    e2 = timedelta(hours=end2.hour, minutes=end2.minute)
    return s1 < e2 and s2 < e1


def add_audit_log(actor, action, target_type='', target_id=None, detail='', ip=''):
    log = AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        ip_address=ip
    )
    db.session.add(log)
    db.session.commit()
    return log


def add_status_history(application, from_status, to_status, operator='', action='', comment=''):
    h = StatusHistory(
        application_id=application.id,
        from_status=from_status,
        to_status=to_status,
        operator=operator,
        action=action,
        comment=comment
    )
    db.session.add(h)


def check_time_within_venue_hours(venue, start_t, end_t):
    return start_t >= venue.open_time and end_t <= venue.close_time


def check_conflict(venue_id, apply_date, start_t, end_t, exclude_app_id=None):
    query = Application.query.filter(
        Application.venue_id == venue_id,
        Application.apply_date == apply_date,
        Application.status == ApplicationStatus.CONFIRMED
    )
    if exclude_app_id:
        query = query.filter(Application.id != exclude_app_id)

    confirmed_apps = query.all()
    for app in confirmed_apps:
        if time_overlap(start_t, end_t, app.start_time, app.end_time):
            return app
    return None


def check_daily_quota(venue, apply_date, exclude_app_id=None):
    query = Application.query.filter(
        Application.venue_id == venue.id,
        Application.apply_date == apply_date,
        Application.status == ApplicationStatus.CONFIRMED
    )
    if exclude_app_id:
        query = query.filter(Application.id != exclude_app_id)
    count = query.count()
    return count < venue.daily_quota, count


def check_pending_conflict(venue_id, apply_date, start_t, end_t, exclude_app_id=None):
    query = Application.query.filter(
        Application.venue_id == venue_id,
        Application.apply_date == apply_date,
        Application.status.in_([ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.CONFIRMED])
    )
    if exclude_app_id:
        query = query.filter(Application.id != exclude_app_id)

    apps = query.all()
    for a in apps:
        if time_overlap(start_t, end_t, a.start_time, a.end_time):
            return a
    return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/auth/info', methods=['GET'])
def auth_info():
    name = request.args.get('name', '').strip()
    return jsonify({
        'name': name,
        'is_approver': is_approver(name),
        'approvers': sorted(list(APPROVERS))
    })


@app.route('/api/venues', methods=['GET'])
def list_venues():
    venues = Venue.query.order_by(Venue.id.asc()).all()
    return jsonify([v.to_dict() for v in venues])


@app.route('/api/venues', methods=['POST'])
def create_venue():
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '场地名称不能为空'}), 400

    open_time = parse_time_str(data.get('open_time', '09:00'))
    close_time = parse_time_str(data.get('close_time', '18:00'))
    daily_quota = int(data.get('daily_quota', 1))

    if open_time >= close_time:
        return jsonify({'error': '开放时间必须早于关闭时间'}), 400
    if daily_quota < 1:
        return jsonify({'error': '日配额至少为1'}), 400

    venue = Venue(
        name=name,
        description=data.get('description', ''),
        capacity=int(data.get('capacity', 0)),
        open_time=open_time,
        close_time=close_time,
        daily_quota=daily_quota,
        is_active=bool(data.get('is_active', True))
    )
    db.session.add(venue)
    db.session.commit()

    add_audit_log(data.get('operator', 'admin'), 'create_venue', 'venue', venue.id,
                  f'创建场地: {name}', request.remote_addr)

    return jsonify(venue.to_dict()), 201


@app.route('/api/venues/<int:venue_id>', methods=['GET'])
def get_venue(venue_id):
    venue = Venue.query.get(venue_id)
    if not venue:
        return jsonify({'error': '场地不存在'}), 404
    return jsonify(venue.to_dict())


@app.route('/api/venues/<int:venue_id>', methods=['PUT'])
def update_venue(venue_id):
    venue = Venue.query.get(venue_id)
    if not venue:
        return jsonify({'error': '场地不存在'}), 404

    data = request.get_json()

    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': '场地名称不能为空'}), 400
        venue.name = name

    if 'description' in data:
        venue.description = data['description']
    if 'capacity' in data:
        venue.capacity = int(data['capacity'])
    if 'is_active' in data:
        venue.is_active = bool(data['is_active'])

    if 'open_time' in data:
        venue.open_time = parse_time_str(data['open_time'])
    if 'close_time' in data:
        venue.close_time = parse_time_str(data['close_time'])
    if 'daily_quota' in data:
        dq = int(data['daily_quota'])
        if dq < 1:
            return jsonify({'error': '日配额至少为1'}), 400
        venue.daily_quota = dq

    if venue.open_time >= venue.close_time:
        return jsonify({'error': '开放时间必须早于关闭时间'}), 400

    db.session.commit()

    add_audit_log(data.get('operator', 'admin'), 'update_venue', 'venue', venue.id,
                  f'更新场地: {venue.name}', request.remote_addr)

    return jsonify(venue.to_dict())


@app.route('/api/venues/<int:venue_id>', methods=['DELETE'])
def delete_venue(venue_id):
    venue = Venue.query.get(venue_id)
    if not venue:
        return jsonify({'error': '场地不存在'}), 404

    has_apps = Application.query.filter_by(venue_id=venue_id).count() > 0
    if has_apps:
        venue.is_active = False
        db.session.commit()
        add_audit_log(request.args.get('operator', 'admin'), 'deactivate_venue', 'venue', venue_id,
                      f'停用场地（有历史申请，软删除）: {venue.name}', request.remote_addr)
        return jsonify({'message': '场地已停用（存在历史申请，软删除）', 'venue': venue.to_dict()})

    db.session.delete(venue)
    db.session.commit()

    add_audit_log(request.args.get('operator', 'admin'), 'delete_venue', 'venue', venue_id,
                  f'删除场地: {venue.name}', request.remote_addr)

    return jsonify({'message': '场地已删除'})


@app.route('/api/applications', methods=['GET'])
def list_applications():
    venue_id = request.args.get('venue_id', type=int)
    status = request.args.get('status')
    apply_date = request.args.get('apply_date')

    query = Application.query
    if venue_id:
        query = query.filter_by(venue_id=venue_id)
    if status:
        query = query.filter_by(status=status)
    if apply_date:
        query = query.filter_by(apply_date=parse_date_str(apply_date))

    apps = query.order_by(Application.id.desc()).all()
    return jsonify([a.to_dict() for a in apps])


@app.route('/api/applications', methods=['POST'])
def create_application():
    data = request.get_json()

    venue_id = data.get('venue_id')
    venue = Venue.query.get(venue_id)
    if not venue:
        return jsonify({'error': '场地不存在'}), 404
    if not venue.is_active:
        return jsonify({'error': '场地已停用，不能申请'}), 400

    apply_date = parse_date_str(data.get('apply_date'))
    start_t = parse_time_str(data.get('start_time'))
    end_t = parse_time_str(data.get('end_time'))

    if start_t >= end_t:
        return jsonify({'error': '开始时间必须早于结束时间'}), 400

    if not check_time_within_venue_hours(venue, start_t, end_t):
        return jsonify({
            'error': f'申请时段超出场地营业时间（{venue.open_time.strftime("%H:%M")}-{venue.close_time.strftime("%H:%M")}）'
        }), 400

    applicant_name = data.get('applicant_name', '').strip()
    event_name = data.get('event_name', '').strip()
    if not applicant_name:
        return jsonify({'error': '申请人不能为空'}), 400
    if not event_name:
        return jsonify({'error': '活动名称不能为空'}), 400

    app = Application(
        venue_id=venue_id,
        applicant_name=applicant_name,
        applicant_phone=data.get('applicant_phone', ''),
        event_name=event_name,
        event_description=data.get('event_description', ''),
        participants=int(data.get('participants', 0)),
        apply_date=apply_date,
        start_time=start_t,
        end_time=end_t,
        status=ApplicationStatus.SUBMITTED,
        created_by=data.get('created_by', applicant_name)
    )
    db.session.add(app)
    db.session.flush()

    add_status_history(app, None, ApplicationStatus.SUBMITTED,
                       operator=data.get('created_by', applicant_name),
                       action='submit',
                       comment='提交申请')

    app.status = ApplicationStatus.PENDING_APPROVAL
    add_status_history(app, ApplicationStatus.SUBMITTED, ApplicationStatus.PENDING_APPROVAL,
                       operator='system',
                       action='auto_route',
                       comment='系统自动进入待审批')

    db.session.commit()

    add_audit_log(data.get('created_by', applicant_name), 'create_application', 'application', app.id,
                  f'提交申请: {event_name} ({apply_date.isoformat()} {start_t.strftime("%H:%M")}-{end_t.strftime("%H:%M")})',
                  request.remote_addr)

    return jsonify(app.to_dict(include_history=True)), 201


@app.route('/api/applications/<int:app_id>', methods=['GET'])
def get_application(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404
    return jsonify(app.to_dict(include_history=True))


@app.route('/api/applications/<int:app_id>/approve', methods=['POST'])
def approve_application(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    data = request.get_json() or {}
    operator = data.get('operator', 'admin')

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'approve_denied', 'application', app_id,
                      '无权限审批被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    if app.status not in [ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED]:
        return jsonify({'error': f'当前状态为 {app.status}，不能审批通过'}), 400

    conflict_app = check_conflict(app.venue_id, app.apply_date, app.start_time, app.end_time, exclude_app_id=app.id)
    if conflict_app:
        return jsonify({
            'error': f'时段冲突：与申请 #{conflict_app.id}「{conflict_app.event_name}」时间重叠',
            'conflict_with': {
                'id': conflict_app.id,
                'event_name': conflict_app.event_name,
                'start_time': conflict_app.start_time.strftime('%H:%M'),
                'end_time': conflict_app.end_time.strftime('%H:%M'),
            }
        }), 409

    quota_ok, current_count = check_daily_quota(app.venue, app.apply_date, exclude_app_id=app.id)
    if not quota_ok:
        return jsonify({
            'error': f'超出当日配额（已确认 {current_count} 场，配额 {app.venue.daily_quota} 场）'
        }), 409

    from_status = app.status
    app.status = ApplicationStatus.CONFIRMED
    app.previous_status = from_status
    app.approval_comment = data.get('comment', '')
    app.approved_by = operator
    app.approved_at = datetime.utcnow()

    add_status_history(app, from_status, ApplicationStatus.CONFIRMED,
                       operator=operator,
                       action='approve',
                       comment=data.get('comment', '审批通过'))

    db.session.commit()

    add_audit_log(operator, 'approve_application', 'application', app.id,
                  f'审批通过申请 #{app.id}: {app.event_name}', request.remote_addr)

    return jsonify(app.to_dict(include_history=True))


@app.route('/api/applications/<int:app_id>/reject', methods=['POST'])
def reject_application(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    data = request.get_json() or {}
    operator = data.get('operator', 'admin')
    reason = data.get('reason', '')

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'reject_denied', 'application', app_id,
                      '无权限驳回被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    if app.status not in [ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED]:
        return jsonify({'error': f'当前状态为 {app.status}，不能驳回'}), 400

    from_status = app.status
    app.status = ApplicationStatus.REJECTED
    app.previous_status = from_status
    app.approval_comment = reason
    app.approved_by = operator
    app.approved_at = datetime.utcnow()

    add_status_history(app, from_status, ApplicationStatus.REJECTED,
                       operator=operator,
                       action='reject',
                       comment=reason or '审批驳回')

    db.session.commit()

    add_audit_log(operator, 'reject_application', 'application', app.id,
                  f'驳回申请 #{app.id}: {app.event_name}, 原因: {reason}', request.remote_addr)

    return jsonify(app.to_dict(include_history=True))


@app.route('/api/applications/<int:app_id>/cancel', methods=['POST'])
def cancel_application(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    data = request.get_json() or {}
    operator = data.get('operator', app.applicant_name)
    reason = data.get('reason', '')

    if not is_approver(operator) and operator.strip() != app.applicant_name.strip():
        add_audit_log(operator, 'cancel_denied', 'application', app_id,
                      '无权限取消被拒绝', request.remote_addr)
        return jsonify({'error': '无权取消该申请，仅申请人本人或审批人可取消'}), 403

    if app.status in [ApplicationStatus.CANCELLED, ApplicationStatus.REJECTED]:
        return jsonify({'error': f'当前状态为 {app.status}，不能取消'}), 400

    from_status = app.status
    app.previous_status = from_status
    app.status = ApplicationStatus.CANCELLED
    app.cancel_reason = reason
    app.cancelled_by = operator

    add_status_history(app, from_status, ApplicationStatus.CANCELLED,
                       operator=operator,
                       action='cancel',
                       comment=reason or '取消申请')

    db.session.commit()

    add_audit_log(operator, 'cancel_application', 'application', app.id,
                  f'取消申请 #{app.id}: {app.event_name}, 原因: {reason}', request.remote_addr)

    return jsonify(app.to_dict(include_history=True))


@app.route('/api/applications/<int:app_id>/revoke', methods=['POST'])
def revoke_cancellation(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    data = request.get_json() or {}
    operator = data.get('operator', 'admin')

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'revoke_denied', 'application', app_id,
                      '无权限撤销取消被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    if app.status != ApplicationStatus.CANCELLED:
        return jsonify({'error': f'当前状态为 {app.status}，只有已取消的申请可以撤销'}), 400

    if not app.previous_status:
        return jsonify({'error': '没有记录之前的状态，无法撤销'}), 400

    target_status = app.previous_status

    if target_status == ApplicationStatus.CONFIRMED:
        conflict_app = check_conflict(app.venue_id, app.apply_date, app.start_time, app.end_time,
                                      exclude_app_id=app.id)
        if conflict_app:
            return jsonify({
                'error': f'撤销失败：时段冲突，与申请 #{conflict_app.id}「{conflict_app.event_name}」时间重叠',
                'conflict_with': {
                    'id': conflict_app.id,
                    'event_name': conflict_app.event_name,
                }
            }), 409

        quota_ok, current_count = check_daily_quota(app.venue, app.apply_date, exclude_app_id=app.id)
        if not quota_ok:
            return jsonify({
                'error': f'撤销失败：超出当日配额（已确认 {current_count} 场，配额 {app.venue.daily_quota} 场）'
            }), 409

    from_status = app.status
    app.status = target_status
    app.previous_status = from_status

    add_status_history(app, from_status, target_status,
                       operator=operator,
                       action='revoke_cancel',
                       comment=f'撤销取消，恢复为 {target_status}')

    db.session.commit()

    add_audit_log(operator, 'revoke_cancellation', 'application', app.id,
                  f'撤销取消申请 #{app.id}，恢复为 {target_status}', request.remote_addr)

    return jsonify(app.to_dict(include_history=True))


@app.route('/api/schedule/<date_str>', methods=['GET'])
def get_schedule(date_str):
    try:
        d = parse_date_str(date_str)
    except Exception:
        return jsonify({'error': '日期格式错误，应为 YYYY-MM-DD'}), 400

    venues = Venue.query.filter_by(is_active=True).order_by(Venue.id.asc()).all()
    result = []
    for v in venues:
        apps = Application.query.filter(
            Application.venue_id == v.id,
            Application.apply_date == d,
            Application.status == ApplicationStatus.CONFIRMED
        ).order_by(Application.start_time.asc()).all()

        result.append({
            'venue': v.to_dict(),
            'confirmed_count': len(apps),
            'daily_quota': v.daily_quota,
            'applications': [a.to_dict() for a in apps]
        })

    return jsonify({'date': d.isoformat(), 'venues': result})


@app.route('/api/schedule/<date_str>/export', methods=['GET'])
def export_schedule(date_str):
    try:
        d = parse_date_str(date_str)
    except Exception:
        return jsonify({'error': '日期格式错误，应为 YYYY-MM-DD'}), 400

    venues = Venue.query.filter_by(is_active=True).order_by(Venue.id.asc()).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['日期', '场地', '活动名称', '申请人', '开始时间', '结束时间', '参与人数', '状态', '审批人', '审批意见'])

    for v in venues:
        apps = Application.query.filter(
            Application.venue_id == v.id,
            Application.apply_date == d,
            Application.status == ApplicationStatus.CONFIRMED
        ).order_by(Application.start_time.asc()).all()

        for a in apps:
            writer.writerow([
                d.isoformat(),
                v.name,
                a.event_name,
                a.applicant_name,
                a.start_time.strftime('%H:%M'),
                a.end_time.strftime('%H:%M'),
                a.participants,
                '已确认',
                a.approved_by,
                a.approval_comment
            ])

    csv_content = output.getvalue()
    output.close()

    add_audit_log(request.args.get('operator', 'anonymous'), 'export_schedule', 'schedule', None,
                  f'导出 {d.isoformat()} 排期表', request.remote_addr)

    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename=schedule_{d.isoformat()}.csv'}
    )


@app.route('/api/audit-logs', methods=['GET'])
def list_audit_logs():
    limit = request.args.get('limit', 100, type=int)
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


@app.route('/api/applications/<int:app_id>/history', methods=['GET'])
def get_application_history(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404
    return jsonify([h.to_dict() for h in app.status_history])


def init_seed_data():
    if Venue.query.count() > 0:
        return

    venues = [
        {
            'name': '多功能厅A',
            'description': '大型多功能厅，适合举办会议、培训、发布会',
            'capacity': 100,
            'open_time': time(8, 0),
            'close_time': time(20, 0),
            'daily_quota': 3,
        },
        {
            'name': '会议室B',
            'description': '中型会议室，配备投影仪和白板',
            'capacity': 30,
            'open_time': time(9, 0),
            'close_time': time(18, 0),
            'daily_quota': 5,
        },
        {
            'name': '活动室C',
            'description': '小型活动室，适合团队建设和小组讨论',
            'capacity': 15,
            'open_time': time(10, 0),
            'close_time': time(21, 0),
            'daily_quota': 2,
        }
    ]

    for v_data in venues:
        v = Venue(**v_data)
        db.session.add(v)

    db.session.commit()
    print('已初始化示例场地数据')


def create_tables():
    with app.app_context():
        db.create_all()
        init_seed_data()


if __name__ == '__main__':
    app.run(debug=True, port=5001, host='0.0.0.0')
