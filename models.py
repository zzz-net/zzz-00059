import os
from datetime import datetime, time, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

db = SQLAlchemy()


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
