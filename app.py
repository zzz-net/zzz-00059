import os
import json
import csv
from io import StringIO
from datetime import datetime, time, timedelta, date
from flask import Flask, request, jsonify, render_template, Response, send_file

from models import (db, Venue, Application, ApplicationStatus, StatusHistory, AuditLog,
                    ImportBatch, ImportBatchStatus, ImportRecord, ImportRecordStatus,
                    ImportRecordErrorCategory, ERROR_CATEGORY_LABEL,
                    VenueClosure, VenueClosureStatus, VenueClosureWaiver)

app = Flask(__name__, template_folder='templates', static_folder='static')

_db_path = os.environ.get('TEST_DB') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scheduling.db')
if _db_path.startswith('sqlite://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_path
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + _db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_AS_ASCII'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'check_same_thread': False, 'timeout': 30},
    'pool_pre_ping': True,
}
app.config['SQLALCHEMY_POOL_RECYCLE'] = 3600

APPROVERS = {'张三', '管理员', 'admin', 'Administrator'}


def _is_cancelled_batch_duplicate(filename, operator):
    existing = ImportBatch.query.filter(
        ImportBatch.filename == filename,
        ImportBatch.created_by == operator,
        ImportBatch.status == ImportBatchStatus.CANCELLED
    ).first()
    return existing

db.init_app(app)

if os.environ.get('TEST_MODE') == 'http_process':
    import traceback

    @app.errorhandler(Exception)
    def handle_exception(e):
        print('\n' + '=' * 80)
        print('[SERVER ERROR] %s: %s' % (type(e).__name__, e))
        traceback.print_exc()
        print('=' * 80 + '\n')
        return jsonify({'error': str(e)}), 500

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

            rs2 = conn.execute(text("PRAGMA table_info(import_records)"))
            cols2 = {row[1] for row in rs2.fetchall()}
            for c in ('error_category', 'conflict_with_application_id'):
                if c not in cols2:
                    col_type2 = 'INTEGER' if c == 'conflict_with_application_id' else 'VARCHAR(50)'
                    conn.execute(text(f"ALTER TABLE import_records ADD COLUMN {c} {col_type2} DEFAULT ''"))

            rs3 = conn.execute(text("PRAGMA table_info(venue_closures)"))
            cols3 = {row[1] for row in rs3.fetchall()}
            if not cols3:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS venue_closures (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        venue_id INTEGER NOT NULL,
                        closure_start_date DATE NOT NULL,
                        closure_end_date DATE NOT NULL,
                        closure_start_time TIME,
                        closure_end_time TIME,
                        reason TEXT DEFAULT '',
                        restore_note TEXT DEFAULT '',
                        affects_existing_applications BOOLEAN DEFAULT 1,
                        created_by VARCHAR(100) DEFAULT '',
                        created_at DATETIME,
                        updated_at DATETIME,
                        status VARCHAR(30) DEFAULT 'active',
                        revoked_by VARCHAR(100) DEFAULT '',
                        revoked_at DATETIME,
                        revoke_reason TEXT DEFAULT '',
                        FOREIGN KEY (venue_id) REFERENCES venues(id)
                    )
                """))
            else:
                for c in ('affects_existing_applications', 'status', 'revoked_by', 'revoked_at', 'revoke_reason', 'restore_note'):
                    if c not in cols3:
                        if c == 'affects_existing_applications':
                            col_t = 'BOOLEAN DEFAULT 1'
                        elif c in ('revoked_at',):
                            col_t = 'DATETIME'
                        elif c == 'status':
                            col_t = "VARCHAR(30) DEFAULT 'active'"
                        elif c in ('restore_note', 'revoke_reason'):
                            col_t = 'TEXT DEFAULT \'\''
                        else:
                            col_t = 'VARCHAR(100) DEFAULT \'\''
                        conn.execute(text(f"ALTER TABLE venue_closures ADD COLUMN {c} {col_t}"))

            rs4 = conn.execute(text("PRAGMA table_info(venue_closure_waivers)"))
            cols4 = {row[1] for row in rs4.fetchall()}
            if not cols4:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS venue_closure_waivers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        closure_id INTEGER NOT NULL,
                        application_id INTEGER NOT NULL,
                        waived_by VARCHAR(100) DEFAULT '',
                        waived_at DATETIME,
                        waiver_reason TEXT DEFAULT '',
                        created_at DATETIME,
                        FOREIGN KEY (closure_id) REFERENCES venue_closures(id),
                        FOREIGN KEY (application_id) REFERENCES applications(id)
                    )
                """))
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
    error_categories = []
    venue = None
    conflict_application_id = None

    if not record.venue_name.strip():
        errors.append('场地名称不能为空')
        error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)
    else:
        venue = venues_by_name.get(record.venue_name.strip())
        if not venue:
            errors.append(f'场地「{record.venue_name}」不存在')
            error_categories.append(ImportRecordErrorCategory.VENUE_NOT_FOUND)
        elif not venue.is_active:
            errors.append(f'场地「{record.venue_name}」已停用')
            error_categories.append(ImportRecordErrorCategory.VENUE_INACTIVE)
        else:
            record.venue_id = venue.id

    if not record.event_name.strip():
        errors.append('活动名称不能为空')
        error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)

    if not record.applicant_name.strip():
        errors.append('申请人不能为空')
        error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)

    if not record.apply_date:
        errors.append('申请日期格式错误，应为 YYYY-MM-DD')
        error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)

    if not record.start_time or not record.end_time:
        errors.append('时间格式错误，应为 HH:MM')
        error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)
    else:
        if record.start_time >= record.end_time:
            errors.append('开始时间必须早于结束时间')
            error_categories.append(ImportRecordErrorCategory.VALIDATION_ERROR)

    if venue and record.start_time and record.end_time and not errors:
        if not check_time_within_venue_hours(venue, record.start_time, record.end_time):
            errors.append(
                f'申请时段超出场地营业时间（{venue.open_time.strftime("%H:%M")}-{venue.close_time.strftime("%H:%M")}）')
            error_categories.append(ImportRecordErrorCategory.INVALID_HOURS)

    if venue and record.apply_date and record.start_time and record.end_time and not errors:
        conflict_app = check_pending_conflict(venue.id, record.apply_date,
                                              record.start_time, record.end_time)
        if conflict_app:
            status_text = '已确认' if conflict_app.status == ApplicationStatus.CONFIRMED else '待审批'
            errors.append(
                f'时段冲突：与{status_text}申请 #{conflict_app.id}「{conflict_app.event_name}」时间重叠')
            error_categories.append(ImportRecordErrorCategory.TIME_CONFLICT)
            conflict_application_id = conflict_app.id

    if venue and record.apply_date and not errors:
        quota_ok, current_count = check_daily_quota(venue, record.apply_date)
        if not quota_ok:
            errors.append(
                f'超出当日配额（已确认 {current_count} 场，配额 {venue.daily_quota} 场）')
            error_categories.append(ImportRecordErrorCategory.QUOTA_EXCEEDED)

    if venue and record.apply_date and record.start_time and record.end_time and not errors:
        closure = find_active_venue_closure(venue.id, record.apply_date, record.start_time, record.end_time)
        if closure:
            cs = closure.closure_start_time or time(0, 0)
            ce = closure.closure_end_time or time(23, 59)
            t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
            errors.append(
                f'场地临时封场：{closure.reason or "场地维护"}（{t_range}），申请时段在封场范围内'
            )
            error_categories.append(ImportRecordErrorCategory.VENUE_CLOSED)

    primary_category = error_categories[0] if error_categories else ''
    return errors, venue, primary_category, conflict_application_id


def preview_import_batch(batch_id):
    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return None, '批次不存在'

    venues = Venue.query.all()
    venues_by_name = {v.name: v for v in venues}

    records = ImportRecord.query.filter_by(batch_id=batch_id).order_by(
        ImportRecord.line_number).all()

    for idx, record in enumerate(records):
        errors, venue, error_category, conflict_app_id = validate_import_record(record, venues_by_name)
        if errors:
            record.status = ImportRecordStatus.PREVIEW_FAIL
            record.error_message = '；'.join(errors)
            record.error_category = error_category
            record.conflict_with_application_id = conflict_app_id
        else:
            record.status = ImportRecordStatus.PREVIEW_PASS
            record.error_category = ''
            record.conflict_with_application_id = None

    duplicates = _check_csv_duplicates_in_batch(records)
    duplicate_set = set()
    for idx1, idx2 in duplicates:
        duplicate_set.add(idx1)
        duplicate_set.add(idx2)
        if records[idx1].status == ImportRecordStatus.PREVIEW_PASS:
            records[idx1].status = ImportRecordStatus.DUPLICATE_IN_BATCH
            records[idx1].error_message = '与同文件内第 %d 行重复（同场地同时段）' % (records[idx2].line_number)
            records[idx1].error_category = ImportRecordErrorCategory.DUPLICATE_IN_BATCH
        if records[idx2].status == ImportRecordStatus.PREVIEW_PASS:
            records[idx2].status = ImportRecordStatus.DUPLICATE_IN_BATCH
            records[idx2].error_message = '与同文件内第 %d 行重复（同场地同时段）' % (records[idx1].line_number)
            records[idx2].error_category = ImportRecordErrorCategory.DUPLICATE_IN_BATCH

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

    records_to_process = ImportRecord.query.filter_by(batch_id=batch_id).order_by(
        ImportRecord.line_number).all()
    for record in records_to_process:
        if record.status == ImportRecordStatus.IMPORT_SUCCESS:
            success_count += 1
            continue

        if record.status in (ImportRecordStatus.PREVIEW_FAIL, ImportRecordStatus.DUPLICATE_IN_BATCH):
            record.status = ImportRecordStatus.IMPORT_FAIL
            failed_count += 1
            failure_details.append(f'第{record.line_number}行：{record.error_message}')
            continue

        try:
            errors, venue, error_category, conflict_app_id = validate_import_record(record, venues_by_name)
            if errors:
                record.status = ImportRecordStatus.IMPORT_FAIL
                record.error_message = '；'.join(errors)
                record.error_category = error_category
                record.conflict_with_application_id = conflict_app_id
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
                record.error_category = ImportRecordErrorCategory.TIME_CONFLICT
                record.conflict_with_application_id = final_conflict.id
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
            record.error_category = ''
            record.conflict_with_application_id = None
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
            record.error_category = ImportRecordErrorCategory.SYSTEM_ERROR
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


def find_active_venue_closure(venue_id, apply_date, start_t, end_t, exclude_app_id=None):
    closures = VenueClosure.query.filter(
        VenueClosure.venue_id == venue_id,
        VenueClosure.status == VenueClosureStatus.ACTIVE,
        VenueClosure.closure_start_date <= apply_date,
        VenueClosure.closure_end_date >= apply_date,
    ).all()
    for c in closures:
        if c.covers_period(apply_date, start_t, end_t):
            if exclude_app_id:
                waived = VenueClosureWaiver.query.filter_by(
                    closure_id=c.id,
                    application_id=exclude_app_id
                ).first()
                if waived:
                    continue
            return c
    return None


def has_closure_waiver(closure_id, application_id):
    return VenueClosureWaiver.query.filter_by(
        closure_id=closure_id,
        application_id=application_id
    ).first() is not None


def get_application_waivers(application_id):
    return VenueClosureWaiver.query.filter_by(
        application_id=application_id
    ).order_by(VenueClosureWaiver.id.desc()).all()


def list_active_venue_closures(venue_id=None, apply_date=None):
    query = VenueClosure.query.filter(VenueClosure.status == VenueClosureStatus.ACTIVE)
    if venue_id:
        query = query.filter(VenueClosure.venue_id == venue_id)
    if apply_date:
        query = query.filter(
            VenueClosure.closure_start_date <= apply_date,
            VenueClosure.closure_end_date >= apply_date,
        )
    return query.order_by(VenueClosure.closure_start_date.asc(),
                          VenueClosure.id.asc()).all()


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

    closure = find_active_venue_closure(venue_id, apply_date, start_t, end_t,
                                        exclude_app_id=application.id)
    if closure:
        cs = closure.closure_start_time or time(0, 0)
        ce = closure.closure_end_time or time(23, 59)
        t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
        issues.append(f'场地临时封场：{closure.reason or "场地维护"}（{t_range}）')
        expected = 'closure'

    if confirmed_conflicts:
        issues.append('存在已确认时段冲突')
        if expected == 'pass':
            expected = 'conflict'
    if pending_conflicts:
        issues.append('存在待审批重叠项')
        if expected == 'pass':
            expected = 'warning'
    if not quota_ok:
        issues.append('当日配额已用尽')
        if expected not in ('conflict', 'closure'):
            expected = 'quota_exceeded'

    if application.status not in (ApplicationStatus.PENDING_APPROVAL, ApplicationStatus.SUBMITTED):
        expected = 'not_applicable'
        issues.append('当前申请状态不处于待审批')

    conflict_summary_parts = []
    if closure:
        cs = closure.closure_start_time or time(0, 0)
        ce = closure.closure_end_time or time(23, 59)
        t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
        conflict_summary_parts.append(f'封场拦截：{closure.reason or "场地维护"}（{t_range}）')
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

    precheck_result = {
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
    if closure:
        precheck_result['venue_closure'] = closure.to_dict()
        precheck_result['closure_affects_existing'] = closure.affects_existing_applications
    return precheck_result


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


@app.route('/api/venue-closures/<int:closure_id>/waivers', methods=['POST'])
def create_closure_waiver(closure_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_waiver_create_denied', 'venue_closure', closure_id,
                      '无权限创建封场放行被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    closure = VenueClosure.query.get(closure_id)
    if not closure:
        return jsonify({'error': '封场记录不存在'}), 404
    if closure.status != VenueClosureStatus.ACTIVE:
        return jsonify({'error': '仅生效中的封场可以放行'}), 400

    application_id = data.get('application_id')
    if not application_id:
        return jsonify({'error': '缺少申请ID'}), 400

    app = Application.query.get(application_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    if app.venue_id != closure.venue_id:
        return jsonify({'error': '申请与封场不属于同一场地'}), 400

    if not closure.covers_period(app.apply_date, app.start_time, app.end_time):
        return jsonify({'error': '该申请时段不在封场范围内，无需放行'}), 400

    existing = VenueClosureWaiver.query.filter_by(
        closure_id=closure_id,
        application_id=application_id
    ).first()
    if existing:
        return jsonify({'error': '该申请已存在放行记录'}), 409

    waiver_reason = data.get('waiver_reason', '').strip()

    waiver = VenueClosureWaiver(
        closure_id=closure_id,
        application_id=application_id,
        waived_by=operator,
        waiver_reason=waiver_reason,
    )
    db.session.add(waiver)
    db.session.commit()

    audit_detail = '封场放行：封场#%d 申请#%d (%s) 原因=%s' % (
        closure_id, application_id, app.event_name, waiver_reason or '特殊情况放行'
    )
    add_audit_log(operator, 'create_closure_waiver', 'venue_closure', closure_id,
                  audit_detail, request.remote_addr)
    add_audit_log(operator, 'closure_waiver_granted', 'application', application_id,
                  audit_detail, request.remote_addr)

    return jsonify(waiver.to_dict()), 201


@app.route('/api/venue-closures/<int:closure_id>/waivers/<int:waiver_id>', methods=['DELETE'])
def revoke_closure_waiver(closure_id, waiver_id):
    operator = request.args.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_waiver_revoke_denied', 'venue_closure', closure_id,
                      '无权限撤销封场放行被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    waiver = VenueClosureWaiver.query.get(waiver_id)
    if not waiver or waiver.closure_id != closure_id:
        return jsonify({'error': '放行记录不存在'}), 404

    app = Application.query.get(waiver.application_id)

    db.session.delete(waiver)
    db.session.commit()

    audit_detail = '撤销封场放行：封场#%d 放行#%d 申请#%d' % (
        closure_id, waiver_id, waiver.application_id
    )
    add_audit_log(operator, 'revoke_closure_waiver', 'venue_closure', closure_id,
                  audit_detail, request.remote_addr)
    if app:
        add_audit_log(operator, 'closure_waiver_revoked', 'application', waiver.application_id,
                      audit_detail, request.remote_addr)

    return jsonify({'message': '放行记录已撤销'})


@app.route('/api/applications/<int:app_id>/closure-waivers', methods=['GET'])
def list_application_waivers(app_id):
    viewer = request.args.get('viewer', '').strip()
    ok, err_msg = require_approver(viewer)
    if not ok:
        return jsonify({'error': '无权查看放行记录，需审批人权限'}), 403

    app = Application.query.get(app_id)
    if not app:
        return jsonify({'error': '申请不存在'}), 404

    waivers = get_application_waivers(app_id)
    result = []
    for w in waivers:
        w_dict = w.to_dict()
        if w.closure:
            w_dict['closure'] = w.closure.to_dict(viewer_role='approver')
        result.append(w_dict)
    return jsonify(result)


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

    viewer_role = 'approver' if is_approver(viewer) else 'applicant'

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

    if viewer_role == 'applicant' and viewer:
        query = query.filter(Application.applicant_name == viewer)

    _APPLICANT_STRIP = {
        'approved_by', 'approved_at', 'approval_comment',
        'cancel_reason', 'cancelled_by', 'precheck_result',
        'conflict_summary', 'approval_conclusion',
        'last_precheck_at', 'last_precheck_by', 'previous_status',
    }

    apps = query.order_by(Application.id.desc()).all()
    result = []
    for a in apps:
        d = a.to_dict()
        if viewer_role == 'applicant':
            d = {k: v for k, v in d.items() if k not in _APPLICANT_STRIP}
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

    closure = find_active_venue_closure(venue_id, apply_date, start_t, end_t)
    if closure:
        cs = closure.closure_start_time or time(0, 0)
        ce = closure.closure_end_time or time(23, 59)
        t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
        closure_reason = closure.reason or '场地维护'
        add_audit_log(data.get('created_by', applicant_name), 'application_denied_closure', 'application', None,
                      f'新建申请被封场拦截：{venue.name} {apply_date.isoformat()} {start_t.strftime("%H:%M")}-{end_t.strftime("%H:%M")} 原因={closure_reason}',
                      request.remote_addr)
        return jsonify({
            'error': f'场地临时封场：{closure_reason}（{t_range}），申请时段在封场范围内，无法提交',
            'venue_closure': {
                'closure_id': closure.id,
                'venue_name': venue.name,
                'closure_start_date': closure.closure_start_date.isoformat(),
                'closure_end_date': closure.closure_end_date.isoformat(),
                'closure_start_time': cs.strftime('%H:%M') if closure.closure_start_time else None,
                'closure_end_time': ce.strftime('%H:%M') if closure.closure_end_time else None,
                'reason': closure_reason,
            }
        }), 409

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

    viewer = request.args.get('viewer', '').strip()
    viewer_role = 'approver' if is_approver(viewer) else 'applicant'

    if viewer_role == 'applicant' and viewer and viewer.strip() != app.applicant_name.strip():
        add_audit_log(viewer, 'view_application_denied', 'application', app_id,
                      '普通身份试图查看他人申请被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看该申请详情'}), 403

    data = app.to_dict(include_history=True)

    if viewer_role == 'applicant':
        _APPLICANT_DETAIL_STRIP = {
            'approved_by', 'approved_at', 'approval_comment',
            'cancel_reason', 'cancelled_by', 'precheck_result',
            'conflict_summary', 'approval_conclusion',
            'last_precheck_at', 'last_precheck_by', 'previous_status',
        }
        data = {k: v for k, v in data.items() if k not in _APPLICANT_DETAIL_STRIP}

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

    closure = find_active_venue_closure(app.venue_id, app.apply_date, app.start_time, app.end_time,
                                        exclude_app_id=app.id)
    if closure and closure.affects_existing_applications:
        cs = closure.closure_start_time or time(0, 0)
        ce = closure.closure_end_time or time(23, 59)
        t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
        conclusion = '驳回-场地封场：%s（%s）' % (closure.reason or '场地维护', t_range)
        app.approval_conclusion = conclusion
        db.session.commit()
        add_audit_log(operator, 'approve_closure_block', 'application', app.id,
                      '审批前正式校验封场 | %s' % conclusion, request.remote_addr)
        return jsonify({
            'error': f'场地临时封场：{closure.reason or "场地维护"}（{t_range}），申请时段在封场范围内，无法审批通过',
            'venue_closure': closure.to_dict(),
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

        closure = find_active_venue_closure(app.venue_id, app.apply_date, app.start_time, app.end_time,
                                            exclude_app_id=app.id)
        if closure and closure.affects_existing_applications:
            cs = closure.closure_start_time or time(0, 0)
            ce = closure.closure_end_time or time(23, 59)
            t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
            add_audit_log(operator, 'revoke_closure_block', 'application', app.id,
                          f'撤销取消恢复被封场拦截：{closure.reason or "场地维护"}（{t_range}）',
                          request.remote_addr)
            return jsonify({
                'error': f'撤销失败：场地临时封场：{closure.reason or "场地维护"}（{t_range}），申请时段在封场范围内',
                'venue_closure': closure.to_dict(),
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

    viewer = request.args.get('viewer', '').strip()
    viewer_role = 'approver' if is_approver(viewer) else 'applicant'

    venues = Venue.query.filter_by(is_active=True).order_by(Venue.id.asc()).all()

    active_closures_by_venue = {}
    all_closures = list_active_venue_closures(apply_date=d)
    for c in all_closures:
        active_closures_by_venue.setdefault(c.venue_id, []).append(c)

    result = []
    for v in venues:
        query = Application.query.filter(
            Application.venue_id == v.id,
            Application.apply_date == d,
            Application.status == ApplicationStatus.CONFIRMED
        )
        if viewer_role == 'applicant' and viewer:
            query = query.filter(Application.applicant_name == viewer)
        apps = query.order_by(Application.start_time.asc()).all()

        venue_data = {
            'venue': v.to_dict(),
            'confirmed_count': len(apps),
            'daily_quota': v.daily_quota,
            'applications': []
        }
        closures_for_venue = active_closures_by_venue.get(v.id, [])
        if closures_for_venue and viewer_role == 'approver':
            venue_data['venue_closures'] = [c.to_dict(viewer_role=viewer_role) for c in closures_for_venue]
            affected_app_ids = set()
            for c in closures_for_venue:
                cs = c.closure_start_time or time(0, 0)
                ce = c.closure_end_time or time(23, 59)
                for a in apps:
                    if c.covers_period(d, a.start_time, a.end_time):
                        if not has_closure_waiver(c.id, a.id):
                            affected_app_ids.add(a.id)
            venue_data['closure_affected_application_ids'] = sorted(list(affected_app_ids))

        for a in apps:
            ad = a.to_dict()
            if viewer_role == 'applicant':
                _SCHEDULE_APPLICANT_STRIP = {
                    'approved_by', 'approved_at', 'approval_comment',
                    'cancel_reason', 'cancelled_by', 'precheck_result',
                    'conflict_summary', 'approval_conclusion',
                    'last_precheck_at', 'last_precheck_by', 'previous_status',
                }
                ad = {k: v for k, v in ad.items() if k not in _SCHEDULE_APPLICANT_STRIP}
            hit_closure = None
            for c in closures_for_venue:
                if c.covers_period(d, a.start_time, a.end_time):
                    waived = has_closure_waiver(c.id, a.id)
                    if not waived:
                        hit_closure = c
                        break
            if hit_closure:
                ad['has_venue_closure'] = True
                ad['venue_closure_reason'] = hit_closure.reason or '场地维护'
                if viewer_role == 'approver':
                    ad['venue_closure_id'] = hit_closure.id
                    ad['closure_affects_existing'] = hit_closure.affects_existing_applications
            venue_data['applications'].append(ad)
        result.append(venue_data)

    all_closures_visible = []
    if viewer_role == 'approver':
        for c in all_closures:
            all_closures_visible.append(c.to_dict(viewer_role=viewer_role))

    response_data = {
        'date': d.isoformat(),
        'venues': result,
    }
    if viewer_role == 'approver':
        response_data['venue_closures'] = all_closures_visible

    return jsonify(response_data)


STATUS_EXPORT_LABEL = {
    ApplicationStatus.SUBMITTED: '已提交',
    ApplicationStatus.PENDING_APPROVAL: '待审批',
    ApplicationStatus.CONFIRMED: '已确认',
    ApplicationStatus.CANCELLED: '已取消',
    ApplicationStatus.REJECTED: '已驳回',
}


def _build_app_to_batch_map(app_ids):
    if not app_ids:
        return {}
    records = ImportRecord.query.filter(
        ImportRecord.application_id.in_(app_ids)
    ).all()
    batch_ids = [r.batch_id for r in records if r.batch_id]
    batch_map = {}
    if batch_ids:
        batches = ImportBatch.query.filter(ImportBatch.id.in_(batch_ids)).all()
        batch_map = {b.id: b for b in batches}
    app_batch_map = {}
    for r in records:
        batch = batch_map.get(r.batch_id)
        app_batch_map[r.application_id] = {
            'batch_id': r.batch_id,
            'batch_filename': batch.filename if batch else '',
            'line_number': r.line_number,
            'import_status': r.status,
            'error_category': r.error_category,
            'error_message': r.error_message,
        }
    return app_batch_map


@app.route('/api/schedule/<date_str>/export', methods=['GET'])
def export_schedule(date_str):
    try:
        d = parse_date_str(date_str)
    except Exception:
        return jsonify({'error': '日期格式错误，应为 YYYY-MM-DD'}), 400

    batch_id_filter = request.args.get('batch_id', type=int)
    operator = request.args.get('operator', 'anonymous').strip()
    viewer_role = 'approver' if is_approver(operator) else 'applicant'

    venues = Venue.query.filter_by(is_active=True).order_by(Venue.id.asc()).all()

    active_closures_by_venue = {}
    all_closures = list_active_venue_closures(apply_date=d)
    for c in all_closures:
        active_closures_by_venue.setdefault(c.venue_id, []).append(c)

    output = StringIO()
    writer = csv.writer(output)

    if viewer_role == 'approver':
        writer.writerow([
            '日期', '场地', '活动名称', '申请人', '开始时间', '结束时间',
            '参与人数', '状态', '审批人', '审批意见', '冲突摘要', '审批结论',
            '导入批次ID', '导入文件名', 'CSV行号', '导入结果', '导入失败分类', '导入失败原因',
            '是否命中封场', '封场ID', '封场原因', '封场时段'
        ])
    else:
        writer.writerow([
            '日期', '场地', '活动名称', '申请人', '开始时间', '结束时间',
            '参与人数', '状态', '是否命中封场', '封场原因'
        ])

    apps_query = Application.query.filter(Application.apply_date == d)

    if batch_id_filter:
        import_records = ImportRecord.query.filter_by(batch_id=batch_id_filter).all()
        filtered_app_ids = [r.application_id for r in import_records if r.application_id]
        if filtered_app_ids:
            apps_query = apps_query.filter(Application.id.in_(filtered_app_ids))
        else:
            apps_query = apps_query.filter(Application.id == -1)

    if viewer_role == 'applicant':
        apps_query = apps_query.filter(Application.applicant_name == operator)

    all_apps = []
    for v in venues:
        apps = apps_query.filter(
            Application.venue_id == v.id,
        ).order_by(Application.start_time.asc()).all()
        for a in apps:
            all_apps.append((v, a))

    app_ids = [a.id for _, a in all_apps]
    app_batch_map = _build_app_to_batch_map(app_ids)

    for v, a in all_apps:
        batch_info = app_batch_map.get(a.id, {})
        hit_closure = None
        closures_for_venue = active_closures_by_venue.get(v.id, [])
        for c in closures_for_venue:
            if c.covers_period(d, a.start_time, a.end_time):
                waived = has_closure_waiver(c.id, a.id)
                if not waived:
                    hit_closure = c
                    break
        if hit_closure:
            cs = hit_closure.closure_start_time or time(0, 0)
            ce = hit_closure.closure_end_time or time(23, 59)
            closure_time_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if hit_closure.closure_start_time else '全天'
            closure_reason = hit_closure.reason or '场地维护'
            closure_flag = '是'
        else:
            closure_flag = ''
            closure_reason = ''
            closure_time_range = ''
        if viewer_role == 'approver':
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
                batch_info.get('batch_id', '') or '',
                batch_info.get('batch_filename', '') or '',
                batch_info.get('line_number', '') or '',
                batch_info.get('import_status', '') or '',
                ERROR_CATEGORY_LABEL.get(batch_info.get('error_category', ''), '') or '',
                batch_info.get('error_message', '') or '',
                closure_flag,
                hit_closure.id if hit_closure else '',
                closure_reason,
                closure_time_range,
            ])
        else:
            writer.writerow([
                d.isoformat(),
                v.name,
                a.event_name,
                a.applicant_name,
                a.start_time.strftime('%H:%M'),
                a.end_time.strftime('%H:%M'),
                a.participants,
                STATUS_EXPORT_LABEL.get(a.status, a.status),
                closure_flag,
                closure_reason,
            ])

    csv_content = output.getvalue()
    output.close()

    export_detail = f'导出 {d.isoformat()} 排期表（{viewer_role}视角）'
    if viewer_role == 'approver':
        export_detail += '（含冲突摘要、审批结论与批次摘要与封场标记）'
    if batch_id_filter:
        export_detail += f'，按批次#{batch_id_filter}筛选'
    add_audit_log(operator, 'export_schedule', 'schedule', None, export_detail, request.remote_addr)

    filename = f'schedule_{d.isoformat()}'
    if batch_id_filter:
        filename += f'_batch{batch_id_filter}'
    filename += '.csv'

    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/api/import/<int:batch_id>/export', methods=['GET'])
def export_import_batch(batch_id):
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400
    if not is_approver(operator):
        add_audit_log(operator, 'export_batch_denied', 'import_batch', batch_id,
                      '无权限导出批次被拒绝', request.remote_addr)
        return jsonify({'error': '无权导出批次，需审批人权限'}), 403

    try:
        db.session.rollback()
    except Exception:
        pass

    try:
        batch = ImportBatch.query.get(batch_id)
        if not batch:
            return jsonify({'error': '批次不存在'}), 404

        batch_filename = batch.filename
        batch_id_val = batch.id
        batch_total = batch.total_count

        export_records = ImportRecord.query.filter_by(batch_id=batch_id).order_by(
            ImportRecord.line_number).all()

        records_data = []
        for r in export_records:
            records_data.append({
                'line_number': r.line_number,
                'venue_name': r.venue_name,
                'event_name': r.event_name,
                'applicant_name': r.applicant_name,
                'apply_date': r.apply_date,
                'start_time': r.start_time,
                'end_time': r.end_time,
                'participants': r.participants,
                'status': r.status,
                'error_category': r.error_category,
                'error_message': r.error_message,
                'application_id': r.application_id,
                'conflict_with_application_id': r.conflict_with_application_id,
            })

        app_ids = [r['application_id'] for r in records_data if r['application_id']]
        app_map = {}
        if app_ids:
            apps = Application.query.filter(Application.id.in_(app_ids)).all()
            for a in apps:
                app_map[a.id] = {
                    'status': a.status,
                    'approved_by': a.approved_by,
                    'approval_conclusion': a.approval_conclusion,
                }
    finally:
        try:
            db.session.close()
        except Exception:
            pass

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        '批次ID', '文件名', 'CSV行号', '场地名称', '活动名称', '申请人',
        '申请日期', '开始时间', '结束时间', '参与人数',
        '导入状态', '导入失败分类', '导入失败原因',
        '关联申请ID', '申请当前状态', '审批人', '审批结论', '冲突申请ID'
    ])

    for r in records_data:
        app_status_label = ''
        approved_by = ''
        approval_conclusion = ''
        if r['application_id'] and r['application_id'] in app_map:
            app_info = app_map[r['application_id']]
            app_status_label = STATUS_EXPORT_LABEL.get(app_info['status'], app_info['status'])
            approved_by = app_info['approved_by'] or ''
            approval_conclusion = app_info['approval_conclusion'] or ''

        writer.writerow([
            batch_id_val,
            batch_filename,
            r['line_number'],
            r['venue_name'],
            r['event_name'],
            r['applicant_name'],
            r['apply_date'].isoformat() if r['apply_date'] else '',
            r['start_time'].strftime('%H:%M') if r['start_time'] else '',
            r['end_time'].strftime('%H:%M') if r['end_time'] else '',
            r['participants'],
            r['status'],
            ERROR_CATEGORY_LABEL.get(r['error_category'], '') or '',
            r['error_message'] or '',
            r['application_id'] or '',
            app_status_label,
            approved_by,
            approval_conclusion,
            r['conflict_with_application_id'] or '',
        ])

    csv_content = output.getvalue()
    output.close()

    add_audit_log(operator, 'export_import_batch', 'import_batch', batch_id,
                  f'导出批次#{batch_id}复核详情，共{batch_total}条记录', request.remote_addr)

    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename=import_batch_{batch_id}.csv'}
    )


@app.route('/api/my-schedule', methods=['GET'])
def my_schedule_results():
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400

    viewer_role = 'approver' if is_approver(operator) else 'applicant'
    apply_date_filter = request.args.get('apply_date', '').strip()

    query = Application.query.filter(Application.applicant_name == operator)
    if apply_date_filter:
        query = query.filter(Application.apply_date == parse_date_str(apply_date_filter))

    apps = query.order_by(Application.apply_date.desc(), Application.start_time.asc()).all()

    _MY_SCHEDULE_APPLICANT_STRIP = {
        'approved_by', 'approved_at', 'approval_comment',
        'cancel_reason', 'cancelled_by', 'precheck_result',
        'conflict_summary', 'approval_conclusion',
        'last_precheck_at', 'last_precheck_by', 'previous_status',
    }

    result = []
    for a in apps:
        ad = a.to_dict()
        if viewer_role == 'applicant':
            ad = {k: v for k, v in ad.items() if k not in _MY_SCHEDULE_APPLICANT_STRIP}
        result.append(ad)

    return jsonify(result)


@app.route('/api/audit-logs', methods=['GET'])
def list_audit_logs():
    viewer = request.args.get('viewer', '').strip()
    ok, err_msg = require_approver(viewer)
    if not ok:
        add_audit_log(viewer, 'audit_log_list_denied', 'audit_log', None,
                      '普通身份试图查看审计日志被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看审计日志，需审批人权限'}), 403

    limit = request.args.get('limit', 100, type=int)
    target_type = request.args.get('target_type', '').strip()
    target_id = request.args.get('target_id', type=int)
    actor = request.args.get('actor', '').strip()

    query = AuditLog.query
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)
    if target_id is not None:
        query = query.filter(AuditLog.target_id == target_id)
    if actor:
        query = query.filter(AuditLog.actor == actor)

    logs = query.order_by(AuditLog.id.desc()).limit(limit).all()
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

    cancelled_dup = _is_cancelled_batch_duplicate(file.filename, operator)
    if cancelled_dup:
        add_audit_log(operator, 'import_upload_cancelled_dup', 'import_batch', cancelled_dup.id,
                      f'已取消的历史导入再次上传被拦截，原批次#{cancelled_dup.id}', request.remote_addr)
        return jsonify({
            'error': f'该文件（{file.filename}）已存在已取消的导入批次（#{cancelled_dup.id}），属于历史重复，不能再重新建单',
            'duplicate_batch_id': cancelled_dup.id,
            'duplicate_batch_status': 'cancelled',
        }), 409

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

    viewer_role = 'approver' if is_approver(operator) else 'applicant'

    if viewer_role == 'applicant':
        add_audit_log(operator, 'import_list_applicant', 'import_batch', None,
                      '申请人查看自己相关的批次列表', request.remote_addr)

    status_filter = request.args.get('batch_status', '').strip()
    approval_status_filter = request.args.get('approval_status', '').strip()
    result_filter = request.args.get('import_result', '').strip()

    query = ImportBatch.query

    if viewer_role == 'applicant':
        app_ids_for_user = [a.id for a in Application.query.filter_by(applicant_name=operator).all()]
        if app_ids_for_user:
            record_batch_ids = [r.batch_id for r in ImportRecord.query.filter(
                ImportRecord.application_id.in_(app_ids_for_user)).all()]
            query = query.filter(ImportBatch.id.in_(set(record_batch_ids)))
        else:
            query = query.filter(ImportBatch.id == -1)

    if status_filter:
        query = query.filter_by(status=status_filter)

    batches = query.order_by(ImportBatch.id.desc()).all()

    result = []
    for b in batches:
        batch_dict = b.to_dict(viewer_role=viewer_role)

        if viewer_role == 'approver' and approval_status_filter:
            ab = batch_dict.get('approval_breakdown', {})
            if not ab.get(approval_status_filter, 0) > 0:
                continue

        if result_filter:
            if result_filter == 'all_success' and batch_dict.get('failed_count', 0) > 0:
                continue
            if result_filter == 'has_failure' and batch_dict.get('failed_count', 0) == 0:
                continue
            if result_filter == 'all_failed' and batch_dict.get('success_count', 0) > 0:
                continue

        result.append(batch_dict)

    return jsonify(result)


@app.route('/api/import/<int:batch_id>', methods=['GET'])
def get_import_batch(batch_id):
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400

    viewer_role = 'approver' if is_approver(operator) else 'applicant'

    if viewer_role == 'applicant':
        app_ids_for_user = [a.id for a in Application.query.filter_by(applicant_name=operator).all()]
        record_in_batch = ImportRecord.query.filter(
            ImportRecord.batch_id == batch_id,
            ImportRecord.application_id.in_(app_ids_for_user)
        ).first() if app_ids_for_user else None
        if not record_in_batch:
            add_audit_log(operator, 'import_view_denied', 'import_batch', batch_id,
                          '申请人无权查看不相关的批次详情被拒绝', request.remote_addr)
            return jsonify({'error': '无权查看该批次详情'}), 403

    batch = ImportBatch.query.get(batch_id)
    if not batch:
        return jsonify({'error': '批次不存在'}), 404

    record_filter = request.args.get('record_status', '').strip()
    error_category_filter = request.args.get('error_category', '').strip()
    approval_status_filter = request.args.get('approval_status', '').strip()

    batch_dict = batch.to_dict(include_records=True, include_application_detail=True, viewer_role=viewer_role)

    if viewer_role == 'applicant':
        if app_ids_for_user:
            batch_dict['records'] = [r for r in batch_dict.get('records', [])
                                     if r.get('application_id') in app_ids_for_user]

    if record_filter or error_category_filter or approval_status_filter:
        filtered_records = []
        for r in batch_dict.get('records', []):
            if record_filter and r.get('status') != record_filter:
                continue
            if error_category_filter and r.get('error_category') != error_category_filter:
                continue
            if approval_status_filter:
                app_info = r.get('application')
                if not app_info or app_info.get('status') != approval_status_filter:
                    continue
            filtered_records.append(r)
        batch_dict['records'] = filtered_records

    if viewer_role == 'approver':
        related_logs = AuditLog.query.filter(
            AuditLog.target_type == 'import_batch',
            AuditLog.target_id == batch_id
        ).order_by(AuditLog.id.desc()).all()
        batch_dict['related_audit_logs'] = [l.to_dict() for l in related_logs]

        app_ids = [r.get('application_id') for r in batch_dict.get('records', []) if r.get('application_id')]
        if app_ids:
            app_logs = AuditLog.query.filter(
                AuditLog.target_type == 'application',
                AuditLog.target_id.in_(app_ids)
            ).order_by(AuditLog.id.desc()).all()
            batch_dict['related_application_logs'] = [l.to_dict() for l in app_logs]

    return jsonify(batch_dict)


@app.route('/api/import/<int:batch_id>/records/<int:record_id>/logs', methods=['GET'])
def get_import_record_logs(batch_id, record_id):
    operator = request.args.get('operator', '').strip()
    if not operator:
        return jsonify({'error': '缺少操作人参数'}), 400
    if not is_approver(operator):
        return jsonify({'error': '无权查看，需审批人权限'}), 403

    record = ImportRecord.query.get(record_id)
    if not record or record.batch_id != batch_id:
        return jsonify({'error': '记录不存在'}), 404

    result = {}
    if record.application_id:
        app_logs = AuditLog.query.filter(
            AuditLog.target_type == 'application',
            AuditLog.target_id == record.application_id
        ).order_by(AuditLog.id.desc()).all()
        result['application_logs'] = [l.to_dict() for l in app_logs]

        status_histories = StatusHistory.query.filter_by(
            application_id=record.application_id
        ).order_by(StatusHistory.id.desc()).all()
        result['status_history'] = [h.to_dict() for h in status_histories]

    if record.conflict_with_application_id:
        conflict_app = Application.query.get(record.conflict_with_application_id)
        if conflict_app:
            result['conflict_application'] = conflict_app.to_dict(include_history=True)

    return jsonify(result)


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

    preview_records = ImportRecord.query.filter_by(batch_id=batch_id).order_by(
        ImportRecord.line_number).all()
    for record in preview_records:
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


@app.route('/api/venue-closures', methods=['GET'])
def list_venue_closures():
    viewer = request.args.get('viewer', '').strip()
    ok, err_msg = require_approver(viewer)
    if not ok:
        add_audit_log(viewer, 'closure_list_denied', 'venue_closure', None,
                      '普通身份试图列出封场记录被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看封场列表，需审批人权限'}), 403

    venue_id = request.args.get('venue_id', type=int)
    status_filter = request.args.get('status', '').strip()
    apply_date = request.args.get('apply_date', '').strip()

    query = VenueClosure.query
    if venue_id:
        query = query.filter(VenueClosure.venue_id == venue_id)
    if status_filter:
        query = query.filter(VenueClosure.status == status_filter)
    if apply_date:
        d = parse_date_str(apply_date)
        query = query.filter(
            VenueClosure.closure_start_date <= d,
            VenueClosure.closure_end_date >= d,
        )
    closures = query.order_by(VenueClosure.closure_start_date.desc(),
                              VenueClosure.id.desc()).all()
    return jsonify([c.to_dict(viewer_role='approver') for c in closures])


@app.route('/api/venue-closures/<int:closure_id>', methods=['GET'])
def get_venue_closure(closure_id):
    viewer = request.args.get('viewer', '').strip()
    ok, err_msg = require_approver(viewer)
    if not ok:
        add_audit_log(viewer, 'closure_view_denied', 'venue_closure', closure_id,
                      '普通身份试图查看封场详情被拒绝', request.remote_addr)
        return jsonify({'error': '无权查看封场详情，需审批人权限'}), 403

    closure = VenueClosure.query.get(closure_id)
    if not closure:
        return jsonify({'error': '封场记录不存在'}), 404
    result = closure.to_dict(viewer_role='approver')
    affected_apps = []
    if closure.status == VenueClosureStatus.ACTIVE:
        apps = Application.query.filter(
            Application.venue_id == closure.venue_id,
            Application.apply_date >= closure.closure_start_date,
            Application.apply_date <= closure.closure_end_date,
            Application.status.in_([ApplicationStatus.CONFIRMED,
                                    ApplicationStatus.PENDING_APPROVAL,
                                    ApplicationStatus.SUBMITTED]),
        ).order_by(Application.apply_date.asc(),
                   Application.start_time.asc()).all()
        cs = closure.closure_start_time or time(0, 0)
        ce = closure.closure_end_time or time(23, 59)
        for a in apps:
            if closure.covers_period(a.apply_date, a.start_time, a.end_time):
                app_dict = _app_summary_dict(a)
                waived = has_closure_waiver(closure.id, a.id)
                app_dict['has_waiver'] = waived
                if waived:
                    waiver = VenueClosureWaiver.query.filter_by(
                        closure_id=closure.id,
                        application_id=a.id
                    ).first()
                    app_dict['waiver'] = waiver.to_dict() if waiver else None
                affected_apps.append(app_dict)
    result['affected_applications'] = affected_apps
    related_logs = AuditLog.query.filter(
        AuditLog.target_type == 'venue_closure',
        AuditLog.target_id == closure_id,
    ).order_by(AuditLog.id.desc()).all()
    result['audit_logs'] = [l.to_dict() for l in related_logs]

    waivers = VenueClosureWaiver.query.filter_by(
        closure_id=closure_id
    ).order_by(VenueClosureWaiver.id.desc()).all()
    result['waivers'] = [w.to_dict() for w in waivers]

    return jsonify(result)


@app.route('/api/venue-closures', methods=['POST'])
def create_venue_closure():
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_create_denied', 'venue_closure', None,
                      '无权限创建封场记录被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    venue_id = data.get('venue_id')
    venue = Venue.query.get(venue_id)
    if not venue:
        return jsonify({'error': '场地不存在'}), 404

    closure_start_date = parse_date_str(data.get('closure_start_date'))
    closure_end_date = parse_date_str(data.get('closure_end_date'))
    if not closure_start_date or not closure_end_date:
        return jsonify({'error': '封场起止日期不能为空'}), 400
    if closure_start_date > closure_end_date:
        return jsonify({'error': '封场开始日期不能晚于结束日期'}), 400

    closure_start_time_str = data.get('closure_start_time')
    closure_end_time_str = data.get('closure_end_time')
    closure_start_time = parse_time_str(closure_start_time_str) if closure_start_time_str else None
    closure_end_time = parse_time_str(closure_end_time_str) if closure_end_time_str else None
    if bool(closure_start_time) != bool(closure_end_time):
        return jsonify({'error': '封场时段需同时提供开始和结束时间，或都为空表示全天'}), 400
    if closure_start_time and closure_end_time and closure_start_time >= closure_end_time:
        return jsonify({'error': '封场开始时间必须早于结束时间'}), 400

    reason = data.get('reason', '').strip()
    restore_note = data.get('restore_note', '').strip()
    affects_existing = bool(data.get('affects_existing_applications', True))

    closure = VenueClosure(
        venue_id=venue_id,
        closure_start_date=closure_start_date,
        closure_end_date=closure_end_date,
        closure_start_time=closure_start_time,
        closure_end_time=closure_end_time,
        reason=reason,
        restore_note=restore_note,
        affects_existing_applications=affects_existing,
        created_by=operator,
        status=VenueClosureStatus.ACTIVE,
    )
    db.session.add(closure)
    db.session.flush()

    cs = closure_start_time or time(0, 0)
    ce = closure_end_time or time(23, 59)
    t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure_start_time else '全天'
    affected_count = 0
    if affects_existing:
        existing = Application.query.filter(
            Application.venue_id == venue_id,
            Application.apply_date >= closure_start_date,
            Application.apply_date <= closure_end_date,
            Application.status.in_([ApplicationStatus.CONFIRMED,
                                    ApplicationStatus.PENDING_APPROVAL,
                                    ApplicationStatus.SUBMITTED]),
        ).all()
        for a in existing:
            if closure.covers_period(a.apply_date, a.start_time, a.end_time):
                affected_count += 1

    db.session.commit()

    audit_detail = '创建封场：场地=%s 日期=%s~%s 时段=%s 影响现有=%s 影响申请数=%d 原因=%s' % (
        venue.name, closure_start_date.isoformat(), closure_end_date.isoformat(),
        t_range, affects_existing, affected_count, reason or '场地维护'
    )
    add_audit_log(operator, 'create_venue_closure', 'venue_closure', closure.id,
                  audit_detail, request.remote_addr)

    result = closure.to_dict()
    result['affected_application_count'] = affected_count
    return jsonify(result), 201


@app.route('/api/venue-closures/<int:closure_id>', methods=['PUT'])
def update_venue_closure(closure_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_update_denied', 'venue_closure', closure_id,
                      '无权限更新封场记录被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    closure = VenueClosure.query.get(closure_id)
    if not closure:
        return jsonify({'error': '封场记录不存在'}), 404
    if closure.status != VenueClosureStatus.ACTIVE:
        return jsonify({'error': '仅生效中的封场可以修改'}), 400

    changes = []
    if 'closure_start_date' in data:
        new_val = parse_date_str(data['closure_start_date'])
        if new_val and new_val != closure.closure_start_date:
            changes.append(f'开始日期: {closure.closure_start_date.isoformat()} -> {new_val.isoformat()}')
            closure.closure_start_date = new_val
    if 'closure_end_date' in data:
        new_val = parse_date_str(data['closure_end_date'])
        if new_val and new_val != closure.closure_end_date:
            changes.append(f'结束日期: {closure.closure_end_date.isoformat()} -> {new_val.isoformat()}')
            closure.closure_end_date = new_val
    if closure.closure_start_date > closure.closure_end_date:
        db.session.rollback()
        return jsonify({'error': '封场开始日期不能晚于结束日期'}), 400

    if 'closure_start_time' in data or 'closure_end_time' in data:
        new_s = parse_time_str(data['closure_start_time']) if data.get('closure_start_time') else None
        new_e = parse_time_str(data['closure_end_time']) if data.get('closure_end_time') else None
        if bool(new_s) != bool(new_e):
            db.session.rollback()
            return jsonify({'error': '封场时段需同时提供开始和结束时间，或都为空表示全天'}), 400
        if new_s and new_e and new_s >= new_e:
            db.session.rollback()
            return jsonify({'error': '封场开始时间必须早于结束时间'}), 400
        old_s = closure.closure_start_time
        old_e = closure.closure_end_time
        if new_s != old_s or new_e != old_e:
            old_tr = (f'{old_s.strftime("%H:%M")}-{old_e.strftime("%H:%M")}'
                      if old_s else '全天')
            new_tr = f'{new_s.strftime("%H:%M")}-{new_e.strftime("%H:%M")}' if new_s else '全天'
            changes.append(f'时段: {old_tr} -> {new_tr}')
            closure.closure_start_time = new_s
            closure.closure_end_time = new_e

    if 'reason' in data:
        new_val = data['reason'].strip()
        if new_val != closure.reason:
            changes.append(f'原因变更')
            closure.reason = new_val
    if 'restore_note' in data:
        new_val = data['restore_note'].strip()
        if new_val != closure.restore_note:
            changes.append(f'恢复备注变更')
            closure.restore_note = new_val
    if 'affects_existing_applications' in data:
        new_val = bool(data['affects_existing_applications'])
        if new_val != closure.affects_existing_applications:
            changes.append(f'影响现有申请: {closure.affects_existing_applications} -> {new_val}')
            closure.affects_existing_applications = new_val

    if not changes:
        return jsonify(closure.to_dict())

    db.session.commit()
    add_audit_log(operator, 'update_venue_closure', 'venue_closure', closure_id,
                  f'更新封场：{"；".join(changes)}', request.remote_addr)
    return jsonify(closure.to_dict())


@app.route('/api/venue-closures/<int:closure_id>/revoke', methods=['POST'])
def revoke_venue_closure(closure_id):
    data = request.get_json() or {}
    operator = data.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_revoke_denied', 'venue_closure', closure_id,
                      '无权限撤销封场记录被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    closure = VenueClosure.query.get(closure_id)
    if not closure:
        return jsonify({'error': '封场记录不存在'}), 404
    if closure.status != VenueClosureStatus.ACTIVE:
        return jsonify({'error': '仅生效中的封场可以撤销'}), 400

    closure.status = VenueClosureStatus.REVOKED
    closure.revoked_by = operator
    closure.revoked_at = datetime.utcnow()
    closure.revoke_reason = data.get('revoke_reason', '').strip()
    db.session.commit()

    cs = closure.closure_start_time or time(0, 0)
    ce = closure.closure_end_time or time(23, 59)
    t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
    audit_detail = '撤销封场 #%d：场地=%s 日期=%s~%s 时段=%s 原因=%s' % (
        closure_id, closure.venue.name if closure.venue else '',
        closure.closure_start_date.isoformat(), closure.closure_end_date.isoformat(),
        t_range, closure.revoke_reason or '提前恢复'
    )
    add_audit_log(operator, 'revoke_venue_closure', 'venue_closure', closure_id,
                  audit_detail, request.remote_addr)
    return jsonify(closure.to_dict())


@app.route('/api/venue-closures/<int:closure_id>/delete', methods=['POST'])
@app.route('/api/venue-closures/<int:closure_id>', methods=['DELETE'])
def delete_venue_closure(closure_id):
    if request.method == 'POST':
        data = request.get_json() or {}
        operator = data.get('operator', '').strip()
    else:
        operator = request.args.get('operator', '').strip()
    ok, err_msg = require_approver(operator)
    if not ok:
        add_audit_log(operator, 'closure_delete_denied', 'venue_closure', closure_id,
                      '无权限删除封场记录被拒绝', request.remote_addr)
        return jsonify({'error': err_msg}), 403

    closure = VenueClosure.query.get(closure_id)
    if not closure:
        return jsonify({'error': '封场记录不存在'}), 404

    cs = closure.closure_start_time or time(0, 0)
    ce = closure.closure_end_time or time(23, 59)
    t_range = f'{cs.strftime("%H:%M")}-{ce.strftime("%H:%M")}' if closure.closure_start_time else '全天'
    audit_detail = '删除封场 #%d：场地=%s 日期=%s~%s 时段=%s 状态=%s' % (
        closure_id, closure.venue.name if closure.venue else '',
        closure.closure_start_date.isoformat(), closure.closure_end_date.isoformat(),
        t_range, closure.status
    )

    db.session.delete(closure)
    db.session.commit()

    add_audit_log(operator, 'delete_venue_closure', 'venue_closure', closure_id,
                  audit_detail, request.remote_addr)
    return jsonify({'message': '封场记录已删除'})


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
