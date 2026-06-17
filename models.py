import os
from datetime import datetime, time, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

db = SQLAlchemy()


class VenueClosureStatus:
    ACTIVE = 'active'
    REVOKED = 'revoked'


class VenueClosureWaiver(db.Model):
    __tablename__ = 'venue_closure_waivers'

    id = db.Column(db.Integer, primary_key=True)
    closure_id = db.Column(db.Integer, db.ForeignKey('venue_closures.id'), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'), nullable=False)

    waived_by = db.Column(db.String(100), default='')
    waived_at = db.Column(db.DateTime, default=datetime.utcnow)
    waiver_reason = db.Column(db.Text, default='')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    closure = db.relationship('VenueClosure', backref='waivers', lazy=True)
    application = db.relationship('Application', backref='closure_waivers', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'closure_id': self.closure_id,
            'application_id': self.application_id,
            'waived_by': self.waived_by,
            'waived_at': self.waived_at.isoformat() if self.waived_at else None,
            'waiver_reason': self.waiver_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class VenueClosure(db.Model):
    __tablename__ = 'venue_closures'

    id = db.Column(db.Integer, primary_key=True)
    venue_id = db.Column(db.Integer, db.ForeignKey('venues.id'), nullable=False)

    closure_start_date = db.Column(db.Date, nullable=False)
    closure_end_date = db.Column(db.Date, nullable=False)
    closure_start_time = db.Column(db.Time, nullable=True)
    closure_end_time = db.Column(db.Time, nullable=True)

    reason = db.Column(db.Text, default='')
    restore_note = db.Column(db.Text, default='')
    affects_existing_applications = db.Column(db.Boolean, default=True)

    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    status = db.Column(db.String(30), nullable=False, default=VenueClosureStatus.ACTIVE)
    revoked_by = db.Column(db.String(100), default='')
    revoked_at = db.Column(db.DateTime, default=None)
    revoke_reason = db.Column(db.Text, default='')

    venue = db.relationship('Venue', backref='closures', lazy=True)

    def covers_period(self, apply_date, start_t, end_t):
        if self.status != VenueClosureStatus.ACTIVE:
            return False
        if apply_date < self.closure_start_date or apply_date > self.closure_end_date:
            return False
        cs = self.closure_start_time or time(0, 0)
        ce = self.closure_end_time or time(23, 59)
        s1 = timedelta(hours=start_t.hour, minutes=start_t.minute)
        e1 = timedelta(hours=end_t.hour, minutes=end_t.minute)
        s2 = timedelta(hours=cs.hour, minutes=cs.minute)
        e2 = timedelta(hours=ce.hour, minutes=ce.minute)
        return s1 < e2 and s2 < e1

    def to_dict(self, viewer_role='approver'):
        data = {
            'id': self.id,
            'venue_id': self.venue_id,
            'venue_name': self.venue.name if self.venue else None,
            'closure_start_date': self.closure_start_date.isoformat(),
            'closure_end_date': self.closure_end_date.isoformat(),
            'closure_start_time': self.closure_start_time.strftime('%H:%M') if self.closure_start_time else None,
            'closure_end_time': self.closure_end_time.strftime('%H:%M') if self.closure_end_time else None,
            'reason': self.reason,
            'restore_note': self.restore_note,
            'affects_existing_applications': self.affects_existing_applications,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'status': self.status,
            'revoked_by': self.revoked_by,
            'revoked_at': self.revoked_at.isoformat() if self.revoked_at else None,
            'revoke_reason': self.revoke_reason,
            'status_label': '生效中' if self.status == VenueClosureStatus.ACTIVE else '已撤销',
        }
        if viewer_role == 'applicant':
            _KEEP = {'id', 'venue_id', 'venue_name', 'closure_start_date',
                     'closure_end_date', 'closure_start_time', 'closure_end_time',
                     'reason', 'status', 'status_label'}
            data = {k: v for k, v in data.items() if k in _KEEP}
        return data


class Venue(db.Model):
    __tablename__ = 'venues'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    capacity = db.Column(db.Integer, default=0)

    open_time = db.Column(db.Time, nullable=False)
    close_time = db.Column(db.Time, nullable=False)
    daily_quota = db.Column(db.Integer, nullable=False, default=1)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    applications = db.relationship('Application', backref='venue', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'capacity': self.capacity,
            'open_time': self.open_time.strftime('%H:%M') if self.open_time else None,
            'close_time': self.close_time.strftime('%H:%M') if self.close_time else None,
            'daily_quota': self.daily_quota,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ApplicationStatus:
    SUBMITTED = 'submitted'
    PENDING_APPROVAL = 'pending_approval'
    CONFIRMED = 'confirmed'
    CANCELLED = 'cancelled'
    REJECTED = 'rejected'


class Application(db.Model):
    __tablename__ = 'applications'

    id = db.Column(db.Integer, primary_key=True)
    venue_id = db.Column(db.Integer, db.ForeignKey('venues.id'), nullable=False)

    applicant_name = db.Column(db.String(100), nullable=False)
    applicant_phone = db.Column(db.String(50), default='')
    event_name = db.Column(db.String(200), nullable=False)
    event_description = db.Column(db.Text, default='')
    participants = db.Column(db.Integer, default=0)

    apply_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)

    status = db.Column(db.String(30), nullable=False, default=ApplicationStatus.SUBMITTED)
    previous_status = db.Column(db.String(30), default=None)

    approval_comment = db.Column(db.Text, default='')
    approved_by = db.Column(db.String(100), default='')
    approved_at = db.Column(db.DateTime, default=None)

    cancel_reason = db.Column(db.Text, default='')
    cancelled_by = db.Column(db.String(100), default='')

    precheck_result = db.Column(db.String(30), default='')
    conflict_summary = db.Column(db.Text, default='')
    approval_conclusion = db.Column(db.String(200), default='')
    last_precheck_at = db.Column(db.DateTime, default=None)
    last_precheck_by = db.Column(db.String(100), default='')

    created_by = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    status_history = db.relationship('StatusHistory', backref='application', lazy=True, cascade='all, delete-orphan',
                                     order_by='StatusHistory.created_at')

    def to_dict(self, include_history=False):
        data = {
            'id': self.id,
            'venue_id': self.venue_id,
            'venue_name': self.venue.name if self.venue else None,
            'applicant_name': self.applicant_name,
            'applicant_phone': self.applicant_phone,
            'event_name': self.event_name,
            'event_description': self.event_description,
            'participants': self.participants,
            'apply_date': self.apply_date.isoformat() if self.apply_date else None,
            'start_time': self.start_time.strftime('%H:%M') if self.start_time else None,
            'end_time': self.end_time.strftime('%H:%M') if self.end_time else None,
            'status': self.status,
            'previous_status': self.previous_status,
            'approval_comment': self.approval_comment,
            'approved_by': self.approved_by,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'cancel_reason': self.cancel_reason,
            'cancelled_by': self.cancelled_by,
            'precheck_result': self.precheck_result,
            'conflict_summary': self.conflict_summary,
            'approval_conclusion': self.approval_conclusion,
            'last_precheck_at': self.last_precheck_at.isoformat() if self.last_precheck_at else None,
            'last_precheck_by': self.last_precheck_by,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_history:
            data['status_history'] = [h.to_dict() for h in self.status_history]
        return data


class StatusHistory(db.Model):
    __tablename__ = 'status_history'

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'), nullable=False)

    from_status = db.Column(db.String(30), default=None)
    to_status = db.Column(db.String(30), nullable=False)

    operator = db.Column(db.String(100), default='')
    action = db.Column(db.String(50), default='')
    comment = db.Column(db.Text, default='')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'application_id': self.application_id,
            'from_status': self.from_status,
            'to_status': self.to_status,
            'operator': self.operator,
            'action': self.action,
            'comment': self.comment,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(100), default='')
    action = db.Column(db.String(50), nullable=False)
    target_type = db.Column(db.String(50), default='')
    target_id = db.Column(db.Integer, default=None)
    detail = db.Column(db.Text, default='')
    ip_address = db.Column(db.String(50), default='')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'actor': self.actor,
            'action': self.action,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'detail': self.detail,
            'ip_address': self.ip_address,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ImportBatchStatus:
    PREVIEW = 'preview'
    CONFIRMED = 'confirmed'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


class ImportBatch(db.Model):
    __tablename__ = 'import_batches'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(30), nullable=False, default=ImportBatchStatus.PREVIEW)
    total_count = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    preview_summary = db.Column(db.Text, default='')
    failure_summary = db.Column(db.Text, default='')
    created_by = db.Column(db.String(100), default='')
    confirmed_by = db.Column(db.String(100), default='')
    confirmed_at = db.Column(db.DateTime, default=None)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    records = db.relationship('ImportRecord', backref='batch', lazy=True,
                              cascade='all, delete-orphan', order_by='ImportRecord.line_number')

    def to_dict(self, include_records=False, include_application_detail=False, viewer_role='approver'):
        from collections import Counter

        records_list = list(self.records)

        venue_not_found = 0
        venue_inactive = 0
        invalid_hours = 0
        time_conflict = 0
        quota_exceeded = 0
        duplicate_history = 0
        duplicate_in_batch = 0
        validation_error = 0
        system_error = 0
        venue_closed = 0

        approval_pending = 0
        approval_confirmed = 0
        approval_cancelled = 0
        approval_rejected = 0
        approval_submitted = 0

        for r in records_list:
            if r.error_category == ImportRecordErrorCategory.VENUE_NOT_FOUND:
                venue_not_found += 1
            elif r.error_category == ImportRecordErrorCategory.VENUE_INACTIVE:
                venue_inactive += 1
            elif r.error_category == ImportRecordErrorCategory.INVALID_HOURS:
                invalid_hours += 1
            elif r.error_category == ImportRecordErrorCategory.TIME_CONFLICT:
                time_conflict += 1
            elif r.error_category == ImportRecordErrorCategory.QUOTA_EXCEEDED:
                quota_exceeded += 1
            elif r.error_category == ImportRecordErrorCategory.DUPLICATE_HISTORY:
                duplicate_history += 1
            elif r.error_category == ImportRecordErrorCategory.DUPLICATE_IN_BATCH:
                duplicate_in_batch += 1
            elif r.error_category == ImportRecordErrorCategory.VALIDATION_ERROR:
                validation_error += 1
            elif r.error_category == ImportRecordErrorCategory.SYSTEM_ERROR:
                system_error += 1
            elif r.error_category == ImportRecordErrorCategory.VENUE_CLOSED:
                venue_closed += 1

            if r.application_id:
                app = Application.query.get(r.application_id)
                if app:
                    if app.status == ApplicationStatus.PENDING_APPROVAL:
                        approval_pending += 1
                    elif app.status == ApplicationStatus.CONFIRMED:
                        approval_confirmed += 1
                    elif app.status == ApplicationStatus.CANCELLED:
                        approval_cancelled += 1
                    elif app.status == ApplicationStatus.REJECTED:
                        approval_rejected += 1
                    elif app.status == ApplicationStatus.SUBMITTED:
                        approval_submitted += 1

        data = {
            'id': self.id,
            'filename': self.filename,
            'status': self.status,
            'total_count': self.total_count,
            'success_count': self.success_count,
            'failed_count': self.failed_count,
            'preview_summary': self.preview_summary,
            'failure_summary': self.failure_summary,
            'created_by': self.created_by,
            'confirmed_by': self.confirmed_by,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'error_breakdown': {
                'venue_not_found': venue_not_found,
                'venue_inactive': venue_inactive,
                'invalid_hours': invalid_hours,
                'time_conflict': time_conflict,
                'quota_exceeded': quota_exceeded,
                'duplicate_history': duplicate_history,
                'duplicate_in_batch': duplicate_in_batch,
                'validation_error': validation_error,
                'system_error': system_error,
                'venue_closed': venue_closed,
            },
            'approval_breakdown': {
                'submitted': approval_submitted,
                'pending_approval': approval_pending,
                'confirmed': approval_confirmed,
                'cancelled': approval_cancelled,
                'rejected': approval_rejected,
            },
        }
        if include_records:
            data['records'] = [r.to_dict(include_application_detail=include_application_detail, viewer_role=viewer_role) for r in records_list]
        if viewer_role == 'applicant':
            _BATCH_APPLICANT_STRIP = {
                'id', 'filename', 'created_by', 'confirmed_by',
                'confirmed_at', 'preview_summary', 'failure_summary',
                'error_breakdown', 'approval_breakdown',
            }
            data = {k: v for k, v in data.items() if k not in _BATCH_APPLICANT_STRIP}
        return data


class ImportRecordErrorCategory:
    VENUE_NOT_FOUND = 'venue_not_found'
    VENUE_INACTIVE = 'venue_inactive'
    INVALID_HOURS = 'invalid_hours'
    TIME_CONFLICT = 'time_conflict'
    QUOTA_EXCEEDED = 'quota_exceeded'
    DUPLICATE_HISTORY = 'duplicate_history'
    DUPLICATE_IN_BATCH = 'duplicate_in_batch'
    VALIDATION_ERROR = 'validation_error'
    SYSTEM_ERROR = 'system_error'
    VENUE_CLOSED = 'venue_closed'


ERROR_CATEGORY_LABEL = {
    ImportRecordErrorCategory.VENUE_NOT_FOUND: '场地不存在',
    ImportRecordErrorCategory.VENUE_INACTIVE: '场地已停用',
    ImportRecordErrorCategory.INVALID_HOURS: '营业时间不合法',
    ImportRecordErrorCategory.TIME_CONFLICT: '时段冲突',
    ImportRecordErrorCategory.QUOTA_EXCEEDED: '日配额超限',
    ImportRecordErrorCategory.DUPLICATE_HISTORY: '历史重复',
    ImportRecordErrorCategory.DUPLICATE_IN_BATCH: '批内重复',
    ImportRecordErrorCategory.VALIDATION_ERROR: '校验错误',
    ImportRecordErrorCategory.SYSTEM_ERROR: '系统异常',
    ImportRecordErrorCategory.VENUE_CLOSED: '场地封场',
}


class ImportRecordStatus:
    PENDING = 'pending'
    PREVIEW_PASS = 'preview_pass'
    PREVIEW_FAIL = 'preview_fail'
    IMPORT_SUCCESS = 'import_success'
    IMPORT_FAIL = 'import_fail'
    DUPLICATE_IN_BATCH = 'duplicate_in_batch'


class ImportRecord(db.Model):
    __tablename__ = 'import_records'

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('import_batches.id'), nullable=False)
    line_number = db.Column(db.Integer, nullable=False)

    venue_name = db.Column(db.String(100), default='')
    venue_id = db.Column(db.Integer, default=None)
    event_name = db.Column(db.String(200), default='')
    applicant_name = db.Column(db.String(100), default='')
    apply_date = db.Column(db.Date, default=None)
    start_time = db.Column(db.Time, default=None)
    end_time = db.Column(db.Time, default=None)
    participants = db.Column(db.Integer, default=0)

    status = db.Column(db.String(30), nullable=False, default=ImportRecordStatus.PENDING)
    error_message = db.Column(db.Text, default='')
    error_category = db.Column(db.String(50), default='')
    conflict_with_application_id = db.Column(db.Integer, default=None)
    application_id = db.Column(db.Integer, default=None)

    raw_data = db.Column(db.Text, default='')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, include_application_detail=False, viewer_role='approver'):
        data = {
            'id': self.id,
            'batch_id': self.batch_id,
            'line_number': self.line_number,
            'venue_name': self.venue_name,
            'venue_id': self.venue_id,
            'event_name': self.event_name,
            'applicant_name': self.applicant_name,
            'apply_date': self.apply_date.isoformat() if self.apply_date else None,
            'start_time': self.start_time.strftime('%H:%M') if self.start_time else None,
            'end_time': self.end_time.strftime('%H:%M') if self.end_time else None,
            'participants': self.participants,
            'status': self.status,
            'error_message': self.error_message,
            'error_category': self.error_category,
            'error_category_label': ERROR_CATEGORY_LABEL.get(self.error_category, ''),
            'conflict_with_application_id': self.conflict_with_application_id,
            'application_id': self.application_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_application_detail and self.application_id:
            app = Application.query.get(self.application_id)
            if app:
                data['application'] = {
                    'id': app.id,
                    'status': app.status,
                    'status_label': {
                        'submitted': '已提交',
                        'pending_approval': '待审批',
                        'confirmed': '已确认',
                        'cancelled': '已取消',
                        'rejected': '已驳回',
                    }.get(app.status, app.status),
                    'approved_by': app.approved_by,
                    'approved_at': app.approved_at.isoformat() if app.approved_at else None,
                    'cancelled_by': app.cancelled_by,
                    'cancel_reason': app.cancel_reason,
                    'approval_conclusion': app.approval_conclusion,
                }
        if include_application_detail and self.conflict_with_application_id:
            conflict_app = Application.query.get(self.conflict_with_application_id)
            if conflict_app:
                data['conflict_application'] = {
                    'id': conflict_app.id,
                    'event_name': conflict_app.event_name,
                    'status': conflict_app.status,
                    'status_label': {
                        'submitted': '已提交',
                        'pending_approval': '待审批',
                        'confirmed': '已确认',
                        'cancelled': '已取消',
                        'rejected': '已驳回',
                    }.get(conflict_app.status, conflict_app.status),
                    'apply_date': conflict_app.apply_date.isoformat() if conflict_app.apply_date else None,
                    'start_time': conflict_app.start_time.strftime('%H:%M') if conflict_app.start_time else None,
                    'end_time': conflict_app.end_time.strftime('%H:%M') if conflict_app.end_time else None,
                }
        if viewer_role == 'applicant':
            _RECORD_APPLICANT_STRIP = {
                'batch_id', 'line_number', 'error_category',
                'error_category_label', 'conflict_with_application_id',
                'raw_data',
            }
            data = {k: v for k, v in data.items() if k not in _RECORD_APPLICANT_STRIP}
            if 'application' in data:
                app_dict = data['application']
                _APP_KEEP = {'id', 'status', 'status_label'}
                data['application'] = {k: v for k, v in app_dict.items() if k in _APP_KEEP}
            data.pop('conflict_application', None)
        return data
