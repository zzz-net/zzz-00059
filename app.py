import os
import json
import csv
from io import StringIO
from datetime import datetime, time, timedelta, date
from flask import Flask, request, jsonify, render_template, Response, send_file

from models import (db, Venue, Application, ApplicationStatus, StatusHistory, AuditLog,
                    ImportBatch, ImportBatchStatus, ImportRecord, ImportRecordStatus)

app = Flask(__name__, template_folder='templates', static_folder='static')

_db_path = os.environ.get('TEST_DB') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scheduling.db')
if _db_path.startswith('sqlite://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_path
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + _db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_AS_ASCII'] = False

APPROVERS = {'张三', '管理员', 'admin', 'Administrator'}

db.init_app(app)

_db_initialized = False


def _migrate_schema():
    from sqlalchemy import text
    try:
        conn = db.engine.connect()
        try:
            rs = conn.execute(text("PRAGMA table_info(applications)"))
            cols = {row[1] for row in rs.fetchall()}
            missing = []
            for c in ('precheck_result', 'conflict_summary', 'approval_conclusion',
                      'last_precheck_at', 'last_precheck_by'):
                if c not in cols:
                    missing.append(c)
            for c in missing:
                col_type = 'DATETIME' if c.endswith('_at') else 'VARCHAR(200)' if c == 'approval_conclusion' else 'TEXT' if c == 'conflict_summary' else 'VARCHAR(30)' if c == 'precheck_result' else 'VARCHAR(100)'
                conn.execute(text(f"ALTER TABLE applications ADD COLUMN {c} {col_type} DEFAULT ''"))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _check_csv_duplicates_in_batch(records):
    seen = {}
    duplicates = []
    for idx, record in enumerate(records):
        if record.apply_date and record.start_time and record.end_time and record.venue_id:
            key = (record.venue_id, record.apply_date.isoformat(),
                   record.start_time.strftime('%H:%M'), record.end_time.strftime('%H:%M'))
            if key in seen:
                duplicates.append((idx, seen[key]))
            else:
                seen[key] = idx
    return duplicates


def validate_import_record(record, venues_by_name, daily_usage_cache=None):
    errors = []
    venue = None

    if not record.venue_name.strip():
        errors.append('场地名称不能为空')
    else:
        venue = venues_by_name.get(record.venue_name.strip())
        if not venue:
            errors.append(f'场地「{record.venue_name}」不存在')
        elif not venue.is_active:
            errors.append(f'场地「{record.venue_name}」已停用')
        else:
            record.venue_id = venue.id

    if not record.event_name.strip():
        errors.append('活动名称不能为空')

    if not record.applicant_name.strip():
        errors.append('申请人不能为空')

    if not record.apply_date:
        errors.append('申请日期格式错误，应为 YYYY-MM-DD')

    if not record.start_time or not record.end_time:
        errors.append('时间格式错误，应为 HH:MM')
    else:
        if record.start_time >= record.end_time:
            errors.append('开始时间必须早于结束时间')

    if venue and record.start_time and record.end_time and not errors:
        if not check_time_within_venue_hours(venue, record.start_time, record.end_time):
            errors.append(
                f'申请时段超出场地营业时间（{venue.open_time.strftime("%H:%M")}-{venue.close_time.strftime("%H:%M")}）')

    if venue and record.apply_date and record.start_time and record.end_time and not errors:
        conflict_app = check_pending_conflict(venue.id, record.apply_date,
                                              record.start_time, record.end_time)
        if conflict_app:
            status_text = '已确认' if conflict_app.status == ApplicationStatus.CONFIRMED else '待审批'
            errors.append(
                f'时段冲突：与{status_text}申请 #{conflict_app.id}「{conflict_app.event_name}」时间重叠')

    if venue and record.apply_date and not errors:
        quota_ok, current_count = check_daily_quota(venue, record.apply_date)
        if not quota_ok:
            errors.append(
                f'超出当日配额（已确认 {current_count} 场，配额 {venue.daily_quota} 场）')

    return errors, venue


def preview_import_batch(batch_id):
    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return None, '批次不存在'

    venues = Venue.query.all()
    venues_by_name = {v.name: v for v in venues}

    records = batch.records

    for idx, record in enumerate(records):
        errors, venue = validate_import_record(record, venues_by_name)
        if errors:
            record.status = ImportRecordStatus.PREVIEW_FAIL
            record.error_message = '；'.join(errors)
        else:
            record.status = ImportRecordStatus.PREVIEW_PASS

    duplicates = _check_csv_duplicates_in_batch(records)
    duplicate_set = set()
    for idx1, idx2 in duplicates:
        duplicate_set.add(idx1)
        duplicate_set.add(idx2)
        if records[idx1].status == ImportRecordStatus.PREVIEW_PASS:
            records[idx1].status = ImportRecordStatus.DUPLICATE_IN_BATCH
            records[idx1].error_message = '与同文件内第 %d 行重复（同场地同时段）' % (records[idx2].line_number)
        if records[idx2].status == ImportRecordStatus.PREVIEW_PASS:
            records[idx2].status = ImportRecordStatus.DUPLICATE_IN_BATCH
            records[idx2].error_message = '与同文件内第 %d 行重复（同场地同时段）' % (records[idx1].line_number)

    preview_pass = sum(1 for r in records if r.status == ImportRecordStatus.PREVIEW_PASS)
    preview_fail = sum(1 for r in records if r.status in (ImportRecordStatus.PREVIEW_FAIL, ImportRecordStatus.DUPLICATE_IN_BATCH))

    summary_parts = [
        f'共 {batch.total_count} 条记录',
        f'预演通过 {preview_pass} 条',
        f'预演失败 {preview_fail} 条',
    ]
    batch.preview_summary = '；'.join(summary_parts)

    db.session.commit()
    return batch, None


def execute_import_batch(batch_id, operator):
    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return None, '批次不存在'

    if batch.status != ImportBatchStatus.CONFIRMED:
        return None, '批次未确认，无法执行导入'

    venues = Venue.query.all()
    venues_by_name = {v.name: v for v in venues}

    success_count = 0
    failed_count = 0
    failure_details = []

    for record in batch.records:
        if record.status == ImportRecordStatus.IMPORT_SUCCESS:
            success_count += 1
            continue

        if record.status in (ImportRecordStatus.PREVIEW_FAIL, ImportRecordStatus.DUPLICATE_IN_BATCH):
            record.status = ImportRecordStatus.IMPORT_FAIL
            failed_count += 1
            failure_details.append(f'第{record.line_number}行：{record.error_message}')
            continue

        try:
            errors, venue = validate_import_record(record, venues_by_name)
            if errors:
                record.status = ImportRecordStatus.IMPORT_FAIL
                record.error_message = '；'.join(errors)
                failed_count += 1
                failure_details.append(f'第{record.line_number}行：{record.error_message}')
                db.session.commit()
                continue

            app = Application(
                venue_id=record.venue_id,
                applicant_name=record.applicant_name,
                event_name=record.event_name,
                participants=record.participants,
                apply_date=record.apply_date,
                start_time=record.start_time,
                end_time=record.end_time,
                status=ApplicationStatus.SUBMITTED,
                created_by=operator
            )
            db.session.add(app)
            db.session.flush()

            final_conflict = check_pending_conflict(
                record.venue_id, record.apply_date,
                record.start_time, record.end_time,
                exclude_app_id=app.id
            )
            if final_conflict:
                db.session.rollback()
                status_text = '已确认' if final_conflict.status == ApplicationStatus.CONFIRMED else '待审批'
                final_err = f'写入前兜底检测：与{status_text}申请 #{final_conflict.id}「{final_conflict.event_name}」时间重叠'
                record.status = ImportRecordStatus.IMPORT_FAIL
                record.error_message = final_err
                failed_count += 1
                failure_details.append(f'第{record.line_number}行：{final_err}')
                db.session.commit()
                continue

            add_status_history(app, None, ApplicationStatus.SUBMITTED,
                               operator=operator,
                               action='submit',
                               comment=f'批量导入（批次#{batch.id}）')

            app.status = ApplicationStatus.PENDING_APPROVAL
            add_status_history(app, ApplicationStatus.SUBMITTED, ApplicationStatus.PENDING_APPROVAL,
                               operator='system',
                               action='auto_route',
                               comment='系统自动进入待审批')

            record.application_id = app.id
            record.status = ImportRecordStatus.IMPORT_SUCCESS
            success_count += 1

            db.session.commit()

            add_audit_log(operator, 'import_create_application', 'application', app.id,
                          f'批量导入创建申请: {app.event_name} ({app.apply_date.isoformat()} '
                          f'{app.start_time.strftime("%H:%M")}-{app.end_time.strftime("%H:%M")})，批次#{batch.id}',
                          request.remote_addr)

        except Exception as e:
            db.session.rollback()
            record.status = ImportRecordStatus.IMPORT_FAIL
            record.error_message = f'导入异常：{str(e)}'
            failed_count += 1
            failure_details.append(f'第{record.line_number}行：{record.error_message}')
            db.session.commit()

    batch.success_count = success_count
    batch.failed_count = failed_count
    batch.failure_summary = '；'.join(failure_details[:10])
    if len(failure_details) > 10:
        batch.failure_summary += f'等{len(failure_details)}条'
    batch.status = ImportBatchStatus.COMPLETED
    db.session.commit()

    return batch, None


@app.before_request
def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    with app.app_context():
        db.create_all()
        _migrate_schema()
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


def _app_summary_dict(app):
    return {
        'id': app.id,
        'event_name': app.event_name,
        'applicant_name': app.applicant_name,
        'apply_date': app.apply_date.isoformat() if app.apply_date else None,
        'start_time': app.start_time.strftime('%H:%M') if app.start_time else None,
        'end_time': app.end_time.strftime('%H:%M') if app.end_time else None,
        'status': app.status,
    }


def build_precheck(application):
    venue = application.venue
    if not venue:
        return {
            'application_id': application.id,
            'venue_id': application.venue_id,
            'venue_name': None,
            'apply_date': application.apply_date.isoformat() if application.apply_date else None,
            'start_time': application.start_time.strftime('%H:%M') if application.start_time else None,
            'end_time': application.end_time.strftime('%H:%M') if application.end_time else None,
            'status': application.status,
            'confirmed_count': 0,
            'daily_quota': 0,
            'quota_remaining': 0,
            'quota_ok': False,
            'confirmed_conflicts': [],
            'pending_conflicts': [],
            'confirmed_same_day': [],
            'pending_same_day': [],
            'issues': ['场地不存在或已删除'],
            'expected_result': 'error',
            'conflict_summary': '场地不存在或已删除',
        }

    venue_id = application.venue_id
    apply_date = application.apply_date
    start_t = application.start_time
    end_t = application.end_time

    confirmed_query = Application.query.filter(
        Application.venue_id == venue_id,
        Application.apply_date == apply_date,
        Application.status == ApplicationStatus.CONFIRMED,
        Application.id != application.id
    )
    confirmed_all = confirmed_query.order_by(Application.start_time.asc()).all()
    confirmed_conflicts = [a for a in confirmed_all
                           if time_overlap(start_t, end_t, a.start_time, a.end_time)]

    pending_query = Application.query.filter(
        Application.venue_id == venue_id,
        Application.apply_date == apply_date,
        Application.status == ApplicationStatus.PENDING_APPROVAL,
        Application.id != application.id
    )
    pending_all = pending_query.order_by(Application.start_time.asc()).all()
    pending_conflicts = [a for a in pending_all
                         if time_overlap(start_t, end_t, a.start_time, a.end_time)]

    confirmed_count = confirmed_query.count()
    quota_remaining = max(0, venue.daily_quota - confirmed_count)
    quota_ok = confirmed_count < venue.daily_quota

    issues = []
    expected = 'pass'

    if confirmed_conflicts:
        issues.append('存在已确认时段冲突')
        expected = 'conflict'
    if pending_conflicts:
        issues.append('存在待审批重叠项')
        if expected == 'pass':
            expected = 'warning'
    if not quota_ok:
        issues.append('当日配额已用尽')
        if expected not in ('conflict',):
            expected = 'quota_exceeded'

    if application.status not in (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED):
        expected = 'not_applicable'
        issues.append('当前申请状态不处于待审批')

    conflict_summary_parts = []
    if confirmed_conflicts:
        names = '、'.join('#%d「%s」' % (a.id, a.event_name) for a in confirmed_conflicts[:3])
        if len(confirmed_conflicts) > 3:
            names += '等%d个' % len(confirmed_conflicts)
        conflict_summary_parts.append('已确认冲突：' + names)
    if pending_conflicts:
        names = '、'.join('#%d「%s」' % (a.id, a.event_name) for a in pending_conflicts[:3])
        if len(pending_conflicts) > 3:
            names += '等%d个' % len(pending_conflicts)
        conflict_summary_parts.append('待审批重叠：' + names)
    if not quota_ok:
        conflict_summary_parts.append('配额已满(%d/%d)' % (confirmed_count, venue.daily_quota))
    conflict_summary = '；'.join(conflict_summary_parts)

    return {
        'application_id': application.id,
        'venue_id': venue.id,
        'venue_name': venue.name,
        'apply_date': apply_date.isoformat(),
        'start_time': start_t.strftime('%H:%M'),
        'end_time': end_t.strftime('%H:%M'),
        'status': application.status,
        'confirmed_count': confirmed_count,
        'daily_quota': venue.daily_quota,
        'quota_remaining': quota_remaining,
        'quota_ok': quota_ok,
        'confirmed_conflicts': [_app_summary_dict(a) for a in confirmed_conflicts],
        'pending_conflicts': [_app_summary_dict(a) for a in pending_conflicts],
        'confirmed_same_day': [_app_summary_dict(a) for a in confirmed_all],
        'pending_same_day': [_app_summary_dict(a) for a in pending_all],
        'issues': issues,
        'expected_result': expected,
        'conflict_summary': conflict_summary,
    }


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
    viewer = request.args.get('viewer', '').strip()

    if status == ApplicationStatus.PENDING_APPROVAL and viewer and not is_approver(viewer):
        add_audit_log(viewer, 'list_pending_denied', 'application', None,
                      '普通身份试图列出待审批申请被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看待审批申请列表，需审批人权限'}), 403

    query = Application.query
    if venue_id:
        query = query.filter_by(venue_id=venue_id)
    if status:
        query = query.filter_by(status=status)
    if apply_date:
        query = query.filter_by(apply_date=parse_date_str(apply_date))

    apps = query.order_by(Application.id.desc()).all()
    result = []
    for a in apps:
        d = a.to_dict()
        if is_approver(viewer) and a.status in (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED):
            d['precheck'] = build_precheck(a)
        result.append(d)
    return jsonify(result)


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
    data = app.to_dict(include_history=True)
    viewer = request.args.get('viewer', '').strip()
    if is_approver(viewer) and app.status in (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED):
        data['precheck'] = build_precheck(app)
    return jsonify(data)


@app.route('/api/applications/<int:app_id>/precheck', methods=['GET'])
def precheck_application(app_id):
    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    operator = request.args.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'precheck_denied', 'application', app_id,
                      '无权限执行预检被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    precheck = build_precheck(app)

    app.precheck_result = precheck['expected_result']
    app.conflict_summary = precheck['conflict_summary']
    app.last_precheck_at = datetime.utcnow()
    app.last_precheck_by = operator
    db.session.commit()

    detail_bits = [
        '预检申请 #%d' % app_id,
        '结论=%s' % precheck['expected_result'],
    ]
    if precheck['conflict_summary']:
        detail_bits.append(precheck['conflict_summary'])
    add_audit_log(operator, 'precheck_application', 'application', app_id,
                  ' | '.join(detail_bits), request.remote_addr)

    return jsonify(precheck)


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

    precheck = build_precheck(app)
    app.precheck_result = precheck['expected_result']
    app.conflict_summary = precheck['conflict_summary']
    app.last_precheck_at = datetime.utcnow()
    app.last_precheck_by = operator

    conflict_app = check_conflict(app.venue_id, app.apply_date, app.start_time, app.end_time, exclude_app_id=app.id)
    if conflict_app:
        conclusion = '驳回-时段冲突：与申请 #%d「%s」重叠' % (conflict_app.id, conflict_app.event_name)
        app.approval_conclusion = conclusion
        db.session.commit()
        add_audit_log(operator, 'approve_conflict', 'application', app.id,
                      '审批前正式校验冲突 | %s' % conclusion, request.remote_addr)
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
        conclusion = '驳回-配额已满（已确认%d场/配额%d场）' % (current_count, app.venue.daily_quota)
        app.approval_conclusion = conclusion
        db.session.commit()
        add_audit_log(operator, 'approve_quota_fail', 'application', app.id,
                      '审批前正式校验配额 | %s' % conclusion, request.remote_addr)
        return jsonify({
            'error': f'超出当日配额（已确认 {current_count} 场，配额 {app.venue.daily_quota} 场）'
        }), 409

    from_status = app.status
    app.status = ApplicationStatus.CONFIRMED
    app.previous_status = from_status
    app.approval_comment = data.get('comment', '')
    app.approved_by = operator
    app.approved_at = datetime.utcnow()
    app.approval_conclusion = '审批通过：%s' % data.get('comment', '') if data.get('comment', '') else '审批通过'

    add_status_history(app, from_status, ApplicationStatus.CONFIRMED,
                       operator=operator,
                       action='approve',
                       comment=data.get('comment', '审批通过'))

    db.session.commit()

    audit_detail = '审批通过申请 #%d: %s' % (app.id, app.event_name)
    if precheck['conflict_summary']:
        audit_detail += ' | 预检摘要=' + precheck['conflict_summary']
    add_audit_log(operator, 'approve_application', 'application', app.id,
                  audit_detail, request.remote_addr)

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

    precheck = build_precheck(app)
    app.precheck_result = precheck['expected_result']
    app.conflict_summary = precheck['conflict_summary']
    app.last_precheck_at = datetime.utcnow()
    app.last_precheck_by = operator

    from_status = app.status
    app.status = ApplicationStatus.REJECTED
    app.previous_status = from_status
    app.approval_comment = reason
    app.approved_by = operator
    app.approved_at = datetime.utcnow()
    app.approval_conclusion = '审批驳回：%s' % reason if reason else '审批驳回'

    add_status_history(app, from_status, ApplicationStatus.REJECTED,
                       operator=operator,
                       action='reject',
                       comment=reason or '审批驳回')

    db.session.commit()

    audit_detail = '驳回申请 #%d: %s, 原因: %s' % (app.id, app.event_name, reason)
    if precheck['conflict_summary']:
        audit_detail += ' | 预检摘要=' + precheck['conflict_summary']
    add_audit_log(operator, 'reject_application', 'application', app.id,
                  audit_detail, request.remote_addr)

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


STATUS_EXPORT_LABEL = {
    ApplicationStatus.SUBMITTED: '已提交',
    ApplicationStatus.PENDING_APPROVAL: '待审批',
    ApplicationStatus.CONFIRMED: '已确认',
    ApplicationStatus.CANCELLED: '已取消',
    ApplicationStatus.REJECTED: '已驳回',
}


@app.route('/api/schedule/<date_str>/export', methods=['GET'])
def export_schedule(date_str):
    try:
        d = parse_date_str(date_str)
    except Exception:
        return jsonify({'error': '日期格式错误，应为 YYYY-MM-DD'}), 400

    venues = Venue.query.filter_by(is_active=True).order_by(Venue.id.asc()).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        '日期', '场地', '活动名称', '申请人', '开始时间', '结束时间',
        '参与人数', '状态', '审批人', '审批意见', '冲突摘要', '审批结论'
    ])

    for v in venues:
        apps = Application.query.filter(
            Application.venue_id == v.id,
            Application.apply_date == d,
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
                STATUS_EXPORT_LABEL.get(a.status, a.status),
                a.approved_by or '',
                a.approval_comment or '',
                a.conflict_summary or '',
                a.approval_conclusion or '',
            ])

    csv_content = output.getvalue()
    output.close()

    add_audit_log(request.args.get('operator', 'anonymous'), 'export_schedule', 'schedule', None,
                  f'导出 {d.isoformat()} 排期表（含冲突摘要与审批结论）', request.remote_addr)

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


@app.route('/api/import/upload', methods=['POST'])
def upload_import_csv():
    operator = request.form.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '操作人不能为空'}), 400

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'import_upload_denied', 'import_batch', None,
                      '无权限批量导入被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    if 'file' not in request.files:
        return jsonify({'error': '未上传文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    if not file.filename.endswith('.csv'):
        return jsonify({'error': '仅支持 CSV 文件'}), 400

    try:
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
    except Exception as e:
        return jsonify({'error': f'CSV 解析失败：{str(e)}'}), 400

    if not rows:
        return jsonify({'error': 'CSV 文件为空'}), 400

    required_columns = ['场地名称', '活动名称', '申请人', '申请日期', '开始时间', '结束时间']
    missing_cols = [c for c in required_columns if c not in reader.fieldnames]
    if missing_cols:
        return jsonify({'error': f'缺少必要列：{", ".join(missing_cols)}'}), 400

    batch = ImportBatch(
        filename=file.filename,
        status=ImportBatchStatus.PREVIEW,
        total_count=len(rows),
        created_by=operator
    )
    db.session.add(batch)
    db.session.flush()

    for i, row in enumerate(rows, start=2):
        try:
            apply_date = parse_date_str(row.get('申请日期', '').strip())
        except Exception:
            apply_date = None

        try:
            start_time = parse_time_str(row.get('开始时间', '').strip())
        except Exception:
            start_time = None

        try:
            end_time = parse_time_str(row.get('结束时间', '').strip())
        except Exception:
            end_time = None

        try:
            participants = int(row.get('参与人数', '0') or 0)
        except Exception:
            participants = 0

        record = ImportRecord(
            batch_id=batch.id,
            line_number=i,
            venue_name=row.get('场地名称', '').strip(),
            event_name=row.get('活动名称', '').strip(),
            applicant_name=row.get('申请人', '').strip(),
            apply_date=apply_date,
            start_time=start_time,
            end_time=end_time,
            participants=participants,
            raw_data=json.dumps(row, ensure_ascii=False)
        )
        db.session.add(record)

    db.session.commit()

    preview_import_batch(batch.id)

    add_audit_log(operator, 'import_upload', 'import_batch', batch.id,
                  f'上传导入文件 {file.filename}，共 {len(rows)} 条记录，已完成预演',
                  request.remote_addr)

    return jsonify(batch.to_dict(include_records=True)), 201


@app.route('/api/import', methods=['GET'])
def list_import_batches():
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400
    if not is_approver(operator):
        add_audit_log(operator, 'import_list_denied', 'import_batch', None,
                      '无权限查看导入列表被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看导入列表，需审批人权限'}), 403

    batches = ImportBatch.query.order_by(ImportBatch.id.desc()).all()
    return jsonify([b.to_dict() for b in batches])


@app.route('/api/import/<int:batch_id>', methods=['GET'])
def get_import_batch(batch_id):
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400
    if not is_approver(operator):
        add_audit_log(operator, 'import_view_denied', 'import_batch', batch_id,
                      '无权限查看导入详情被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看导入详情，需审批人权限'}), 403

    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return jsonify({'error': '批次不存在'}), 404
    return jsonify(batch.to_dict(include_records=True))


@app.route('/api/import/<int:batch_id>/preview', methods=['POST'])
def repreview_import_batch(batch_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'import_preview_denied', 'import_batch', batch_id,
                      '无权限重新预演被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return jsonify({'error': '批次不存在'}), 404

    for record in batch.records:
        record.status = ImportRecordStatus.PENDING
        record.error_message = ''

    batch.status = ImportBatchStatus.PREVIEW
    db.session.commit()

    preview_import_batch(batch.id)

    add_audit_log(operator, 'import_preview', 'import_batch', batch_id,
                  f'重新预演导入批次 #{batch_id}', request.remote_addr)

    return jsonify(batch.to_dict(include_records=True))


@app.route('/api/import/<int:batch_id>/confirm', methods=['POST'])
def confirm_import_batch(batch_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'import_confirm_denied', 'import_batch', batch_id,
                      '无权限确认导入被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return jsonify({'error': '批次不存在'}), 404

    if batch.status != ImportBatchStatus.PREVIEW:
        return jsonify({'error': f'批次状态为 {batch.status}，仅预演状态可确认'}), 400

    batch.status = ImportBatchStatus.CONFIRMED
    batch.confirmed_by = operator
    batch.confirmed_at = datetime.utcnow()
    db.session.commit()

    add_audit_log(operator, 'import_confirm', 'import_batch', batch_id,
                  f'确认导入批次 #{batch_id}，准备执行正式导入', request.remote_addr)

    result, err = execute_import_batch(batch_id, operator)
    if err:
        return jsonify({'error': err}), 400

    add_audit_log(operator, 'import_complete', 'import_batch', batch_id,
                  f'导入批次 #{batch_id} 完成：成功 {result.success_count} 条，失败 {result.failed_count} 条',
                  request.remote_addr)

    return jsonify(result.to_dict(include_records=True))


@app.route('/api/import/<int:batch_id>/cancel', methods=['POST'])
def cancel_import_batch(batch_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()

    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'import_cancel_denied', 'import_batch', batch_id,
                      '无权限取消导入被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return jsonify({'error': '批次不存在'}), 404

    if batch.status == ImportBatchStatus.COMPLETED:
        return jsonify({'error': '已完成的批次不能取消'}), 400

    batch.status = ImportBatchStatus.CANCELLED
    db.session.commit()

    add_audit_log(operator, 'import_cancel', 'import_batch', batch_id,
                  f'取消导入批次 #{batch_id}', request.remote_addr)

    return jsonify(batch.to_dict(include_records=True))


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
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=False, port=port, host='0.0.0.0')
