"""
Business rules for LibraryOS, kept out of the views so they are easy to test.

Every mutating function runs inside a transaction and raises BookingError when a
rule is broken. The views turn that into a JSON error response.

Reservation life cycle (every reservation is a time slot: date + start time + end time)
    request  -> PENDING   (queued in its time slot; the Admins are notified. It does not lock the seat: other
                           people may queue other, non-overlapping slots on the same seat)
    accept   -> RESERVED  (approved. The seat switches to "reserved" and the 15-minute grace period starts when
                           the slot's start time arrives - or right away when it is already underway)
    verify   -> ACTIVE    (Admin checked the code; seat is occupied until the slot ends)
    otherwise-> EXPIRED (grace period ran out), REJECTED (Admin declined / request lapsed) or RELEASED
                (cancelled, finished or auto-completed at the slot's end). The next queued slot then takes over.
"""
import logging
import math
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import time as dtime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.db.models.functions import ExtractHour
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time

from .models import (
    OPEN_STATUSES,
    AdminActionLog,
    Notification,
    PasswordResetRequest,
    Profile,
    ProfileChangeRequest,
    Reservation,
    Seat,
    Zone,
)

STUDENT_DURATIONS = (1, 2, 3)
PROFESSOR_DURATIONS = (2, 4, 6)
MAX_TITLE_LENGTH = 100
DEFAULT_PROF_TITLE = "Faculty Group Consultation"
GRACE_PERIOD = timedelta(minutes=15)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid misreading
NOTIFICATIONS_SHOWN = 15

# Time-slot rules
MIN_SLOT = timedelta(minutes=30)
STUDENT_MAX_SLOT = timedelta(hours=3)
PROFESSOR_MAX_SLOT = timedelta(hours=6)
ADMIN_MAX_SLOT = timedelta(hours=12)
MAX_ADVANCE_DAYS = 7
START_TOLERANCE = timedelta(minutes=5)  # a start time a few minutes ago still counts as "now"

Status = Reservation.Status
Role = Reservation.Role
EndReason = Reservation.EndReason
Action = AdminActionLog.Action
User = get_user_model()
logger = logging.getLogger(__name__)


class BookingError(Exception):
    def __init__(self, message, *, status=400, code="error", extra=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


# ----------------------------------------------------------------- helpers
def _lock_seat(seat_id):
    try:
        return Seat.objects.select_for_update().select_related("zone").get(seat_id=seat_id)
    except Seat.DoesNotExist:
        raise BookingError(f"Seat {seat_id} does not exist.", status=404, code="not_found")


def _lock_reservation(reservation_id):
    try:
        pk = int(reservation_id)
    except (TypeError, ValueError):
        raise BookingError("Reservation not found.", status=404, code="not_found")
    reservation = Reservation.objects.select_for_update(of=("self",)).select_related("seat__zone", "user").filter(pk=pk).first()
    if reservation is None:
        raise BookingError("Reservation not found.", status=404, code="not_found")
    return reservation


def _parse_choice(value, allowed, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BookingError(f"Invalid {label}.", code="invalid_input")
    if number not in allowed:
        raise BookingError(f"Invalid {label}.", code="invalid_input")
    return number


def _clean_start_hour(value):
    """Hour of day (0-23) the session starts. The browser sends its local hour."""
    try:
        hour = int(value)
        if 0 <= hour <= 23:
            return hour
    except (TypeError, ValueError):
        pass
    return timezone.localtime().hour


def _ensure_available(seat, *, ignore_pending=False):
    """Legacy helper for reservations that have no time slot: the seat must be free right now."""
    if seat.is_maintenance:
        raise BookingError(f"Seat {seat.seat_id} is under maintenance.", status=409, code="unavailable")
    if seat.current_reservation is not None:
        raise BookingError(f"Seat {seat.seat_id} is no longer available.", status=409, code="unavailable")


# ------------------------------------------------------------- time slots
BLOCKING_STATUSES = [Status.PENDING, *OPEN_STATUSES]


def _max_slot_for(role):
    if role == Role.STUDENT:
        return STUDENT_MAX_SLOT
    if role == Role.ADMIN:
        return ADMIN_MAX_SLOT
    return PROFESSOR_MAX_SLOT


def _hm(dt):
    return timezone.localtime(dt).strftime("%H:%M")


def _fmt_span(start, end):
    return f"{_hm(start)} - {_hm(end)}"


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(day, dtime.min), tz)
    hi = timezone.make_aware(datetime.combine(day + timedelta(days=1), dtime.min), tz)
    return lo, hi


def _parse_slot(date_value, start_value, end_value, *, max_length, now=None):
    """
    Turn the form's Date / Start time / End time into aware datetimes (server time zone).
    Raises BookingError for anything that is not a sensible slot.
    """
    try:
        day = parse_date(str(date_value or "").strip())
        start_t = parse_time(str(start_value or "").strip())
        end_t = parse_time(str(end_value or "").strip())
    except ValueError:
        day = start_t = end_t = None
    if not (day and start_t and end_t):
        raise BookingError("Choose a date, a start time and an end time.", code="invalid_input")

    now = now or timezone.now()
    tz = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(day, start_t.replace(second=0, microsecond=0)), tz)
    end_at = timezone.make_aware(datetime.combine(day, end_t.replace(second=0, microsecond=0)), tz)
    today = timezone.localdate(now)

    if end_at <= start_at:
        raise BookingError("The end time must be later than the start time.", code="invalid_input")
    if end_at - start_at < MIN_SLOT:
        raise BookingError(f"A reservation must be at least {int(MIN_SLOT.total_seconds() // 60)} minutes long.", code="invalid_input")
    if end_at - start_at > max_length:
        hours = max_length.total_seconds() / 3600
        raise BookingError(f"A reservation can be at most {hours:g} hours long.", code="invalid_input")
    if day < today or start_at < now - START_TOLERANCE:
        raise BookingError("That start time has already passed. Pick a time from now onwards.", code="invalid_input")
    if day > today + timedelta(days=MAX_ADVANCE_DAYS):
        raise BookingError(f"Seats can be booked up to {MAX_ADVANCE_DAYS} days ahead.", code="invalid_input")
    return start_at, end_at


def _legacy_slot(duration, start_hour):
    """Old API clients send only a duration (and a start hour): book that many hours starting at that hour today."""
    start = _clean_start_hour(start_hour)
    midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=start), midnight + timedelta(hours=start + duration)


def _resolve_slot(role, allowed_durations, duration, start_hour, date_value, start_value, end_value):
    if date_value or start_value or end_value:
        return _parse_slot(date_value, start_value, end_value, max_length=_max_slot_for(role))
    return _legacy_slot(_parse_choice(duration, allowed_durations, "duration"), start_hour)


def _hour_columns(start_at, end_at):
    """start_hour / end_hour kept for the older columns and reports."""
    start, end = timezone.localtime(start_at), timezone.localtime(end_at)
    return start.hour, end.hour + (1 if end.minute else 0)


def _queue_state(reservation, now=None):
    """(key, label) used by the seat queue: pending | upcoming | reserved (check-in open) | active."""
    now = now or timezone.now()
    if reservation.status == Status.PENDING:
        return "pending", "Pending"
    if reservation.status == Status.ACTIVE:
        return "active", "Active / Checked-in"
    if reservation.is_live(now):
        return "reserved", "Approved - check-in open"
    return "upcoming", "Approved / Upcoming"


def _slot_item(reservation, now, user=None, is_admin=False):
    """One line of a seat's schedule. Other people's names are only shown to Admins."""
    key, label = _queue_state(reservation, now)
    mine = bool(user is not None and reservation.user_id == getattr(user, "id", None))
    if reservation.start_at and reservation.end_at:
        span = _fmt_span(reservation.start_at, reservation.end_at)
        start_iso, end_iso = _iso(reservation.start_at), _iso(reservation.end_at)
    else:  # a walk-in or legacy session with no booked end
        start = reservation.checked_in_at or reservation.created_at
        span = f"{_hm(start)} - now"
        start_iso, end_iso = _iso(start), None
    who = reservation.user_name if (is_admin or mine) else reservation.get_role_display()
    return {
        "id": reservation.pk,
        "start": start_iso,
        "end": end_iso,
        "span": span,
        "status": key,
        "statusLabel": label,
        "mine": mine,
        "who": who,
        "role": reservation.role,
        "title": reservation.title if (is_admin or mine) else "",
    }


def _pending_list(seat):
    cached = getattr(seat, "pending_reservations", None)
    if cached is not None:
        return list(cached)
    return list(seat.reservations.filter(status=Status.PENDING))


def seat_day_queue(seat, day, user=None, is_admin=False, now=None):
    """Every queued slot of a seat on one date (pending, approved/upcoming, active), in chronological order."""
    now = now or timezone.now()
    lo, hi = _day_bounds(day)
    rows = []
    for r in [*seat._open_list(), *_pending_list(seat)]:
        if r.start_at is None or r.end_at is None:
            if r.status == Status.ACTIVE and lo <= (r.checked_in_at or r.created_at) < hi:
                rows.append((r.checked_in_at or r.created_at, r))
        elif r.start_at < hi and r.end_at > lo:
            rows.append((r.start_at, r))
    rows.sort(key=lambda t: (t[0], t[1].pk))
    return [_slot_item(r, now, user, is_admin) for _, r in rows]


def find_slot_conflicts(seat, start_at, end_at, exclude_ids=()):
    """Pending or accepted reservations of `seat` whose slot overlaps [start_at, end_at). Back-to-back slots are fine."""
    return list(
        Reservation.objects.filter(
            seat=seat, status__in=BLOCKING_STATUSES, start_at__lt=end_at, end_at__gt=start_at
        )
        .exclude(pk__in=list(exclude_ids))
        .order_by("start_at", "pk")
    )


def _slot_conflict_error(seat, start_at, end_at, conflicts):
    now = timezone.now()
    day = timezone.localtime(start_at).date()
    taken = seat_day_queue(seat, day, None, False, now)
    clash = ", ".join(f"{_fmt_span(c.start_at, c.end_at)} ({_queue_state(c, now)[1]})" for c in conflicts)
    all_taken = "; ".join(f"{t['span']} ({t['statusLabel']})" for t in taken)
    return BookingError(
        f"Seat {seat.seat_id} is already booked {clash} on {day:%b %d}, which overlaps your "
        f"{_fmt_span(start_at, end_at)} request. Taken slots that day: {all_taken}. Pick a different time.",
        status=409,
        code="slot_conflict",
        extra={"seat": seat.seat_id, "date": day.isoformat(), "taken": taken},
    )


def _ensure_slot_free(seat, start_at, end_at, exclude_ids=()):
    """Raise a conflict error if the slot overlaps a queued reservation, or the seat is blocked / occupied now."""
    if seat.is_maintenance:
        raise BookingError(f"Seat {seat.seat_id} is under maintenance.", status=409, code="unavailable")
    conflicts = find_slot_conflicts(seat, start_at, end_at, exclude_ids)
    if conflicts:
        raise _slot_conflict_error(seat, start_at, end_at, conflicts)
    now = timezone.now()
    if start_at <= now + START_TOLERANCE:  # an immediate slot cannot start while someone is sitting there
        occupant = seat.reservations.filter(status=Status.ACTIVE, start_at__isnull=True).exists()
        if occupant:
            raise BookingError(
                f"Seat {seat.seat_id} is occupied right now (walk-in). Pick a later start time.",
                status=409, code="unavailable",
            )


def get_seat_schedule(user, role, seat_id, day=None):
    """The queue of one seat on one date, for the seat inspector (any signed-in role)."""
    seat = Seat.objects.select_related("zone").filter(seat_id=seat_id).first()
    if seat is None:
        raise BookingError(f"Seat {seat_id} does not exist.", status=404, code="not_found")
    day = day or timezone.localdate()
    now = timezone.now()
    items = seat_day_queue(seat, day, user, role == "admin", now)
    return {
        "seat": seat.seat_id,
        "zone": seat.zone.code,
        "zoneName": seat.zone.name,
        "features": seat.features,
        "status": seat.status,
        "date": day.isoformat(),
        "isToday": day == timezone.localdate(),
        "items": items,
    }


def _create_reservation(**fields):
    try:
        with transaction.atomic():  # savepoint, so an IntegrityError doesn't poison the outer transaction
            return Reservation.objects.create(**fields)
    except IntegrityError:
        raise BookingError("That seat was just taken by someone else.", status=409, code="seat_taken")


def _new_code():
    for _ in range(20):
        code = "LIB-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not Reservation.objects.filter(confirmation_code=code).exists():
            return code
    raise BookingError("Could not generate a confirmation code. Please try again.", status=500, code="error")


def _new_batch_id():
    return secrets.token_hex(6)


def _batch_members(reservation, statuses):
    """The reservation plus, for a group booking, its sibling seats currently in `statuses` (row-locked)."""
    if not reservation.batch_id:
        return [reservation]
    members = list(
        Reservation.objects.select_for_update(of=("self",))
        .select_related("seat__zone", "user")
        .filter(batch_id=reservation.batch_id, status__in=statuses)
        .order_by("seat__seat_id")
    )
    return members or [reservation]


def _seat_list(members):
    ids = [m.seat.seat_id for m in members]
    return (f"Seats {', '.join(ids)}" if len(ids) > 1 else f"Seat {ids[0]}"), len(ids) > 1


def log_admin_action(admin, action, target_name, details="", target_user=None):
    AdminActionLog.objects.create(
        admin=admin,
        admin_username=admin.username if admin else "system",
        action=action,
        target_user=target_user,
        target_name=target_name,
        details=details,
    )


def _close(reservation, reason):
    reservation.status = Status.RELEASED
    reservation.released_at = timezone.now()
    reservation.end_reason = reason
    reservation.save(update_fields=["status", "released_at", "end_reason"])


def _user_end_reason(reservation):
    return EndReason.COMPLETED if reservation.checked_in_at else EndReason.CANCELLED


# ----------------------------------------------------------- notifications
def admin_url(fragment=""):
    return reverse("seats:admin-dashboard") + fragment


def approvals_url(tab="pending"):
    return f"{reverse('seats:approvals')}?tab={tab}"


def user_dashboard_url(user):
    """Where a user's own reservations are shown: the booking tab (student) or sessions tab (professor)."""
    role = getattr(getattr(user, "profile", None), "role", "")
    if role == "professor":
        return reverse("seats:professor-dashboard") + "#sessions"
    if role == "admin":
        return admin_url("#requests")
    return reverse("seats:student-dashboard") + "#bookings"


def notify(user, message, kind="info", reservation=None, url=""):
    if user is not None:
        Notification.objects.create(
            user=user, message=message, kind=kind, reservation=reservation, target_url=url or user_dashboard_url(user)
        )


def notify_admins(message, kind="info", reservation=None, url=""):
    admins = User.objects.filter(is_active=True, profile__role="admin", profile__status="approved")
    Notification.objects.bulk_create(
        [
            Notification(user=a, message=message, kind=kind, reservation=reservation, target_url=url or admin_url("#requests"))
            for a in admins
        ]
    )


def mark_notifications_read(user, ids=None):
    queryset = user.notifications.filter(read_at__isnull=True)
    if ids:
        try:
            queryset = queryset.filter(pk__in=[int(i) for i in ids])
        except (TypeError, ValueError):
            raise BookingError("Invalid notification id.", code="invalid_input")
    queryset.update(read_at=timezone.now())


# ----------------------------------------------------- requests (student/prof)
@transaction.atomic
def reserve_seat_for_student(user, seat_id, duration=None, start_hour=None, *, date=None, start_time=None, end_time=None):
    """
    Queue a time-slot request. The seat is NOT locked for the day: any number of people may queue the same seat
    as long as their [start, end) slots do not overlap a pending / accepted one.
    (`duration` + `start_hour` is the old API: that many hours, starting at that hour today.)
    """
    expire_overdue_reservations()
    seat = _lock_seat(seat_id)

    existing = (
        Reservation.objects.select_related("seat__zone")
        .filter(role=Role.STUDENT, user=user, status__in=[Status.PENDING, *OPEN_STATUSES])
        .first()
    )
    if existing:
        raise BookingError(
            "Students may hold only one pending, active or upcoming reservation at a time.",
            status=409,
            code="single_seat_rule",
            extra={
                "current": {
                    "seat": existing.seat.seat_id,
                    "zoneName": existing.seat.zone.name,
                    "time": existing.time_display,
                }
            },
        )

    start_at, end_at = _resolve_slot(Role.STUDENT, STUDENT_DURATIONS, duration, start_hour, date, start_time, end_time)
    _ensure_slot_free(seat, start_at, end_at)
    start_h, end_h = _hour_columns(start_at, end_at)
    reservation = _create_reservation(
        seat=seat,
        user=user,
        user_name=user.profile.full_name,
        role=Role.STUDENT,
        start_hour=start_h,
        end_hour=end_h,
        start_at=start_at,
        end_at=end_at,
        status=Status.PENDING,  # no confirmation code yet: it is created when an Admin accepts
    )
    notify_admins(
        f"New seat reservation request from {reservation.user_name} for Seat {seat.seat_id} "
        f"({reservation.slot_label}) awaiting approval.",
        reservation=reservation,
    )
    return f"Request sent for Seat {seat.seat_id} ({reservation.slot_label}). Waiting for Admin approval.", "success"


@transaction.atomic
def batch_reserve_for_professor(
    user, seat_ids, title, duration=None, start_hour=None, *, date=None, start_time=None, end_time=None
):
    expire_overdue_reservations()
    if not isinstance(seat_ids, list) or not seat_ids:
        raise BookingError("Please select at least one available seat first.", code="invalid_input")
    seat_ids = list(dict.fromkeys(str(s) for s in seat_ids))
    title = str(title or "").strip()[:MAX_TITLE_LENGTH] or DEFAULT_PROF_TITLE

    seats = list(
        Seat.objects.select_for_update().select_related("zone").filter(seat_id__in=seat_ids).order_by("pk")
    )
    missing = set(seat_ids) - {s.seat_id for s in seats}
    if missing:
        raise BookingError(f"Unknown seat(s): {', '.join(sorted(missing))}.", status=404, code="not_found")

    is_admin = user.profile.role == "admin"
    role = Role.ADMIN if is_admin else Role.PROFESSOR
    start_at, end_at = _resolve_slot(role, PROFESSOR_DURATIONS, duration, start_hour, date, start_time, end_time)

    # all-or-nothing: collect every seat that is not free in this slot and report them together
    blocked = {}
    for seat in seats:
        try:
            _ensure_slot_free(seat, start_at, end_at)
        except BookingError as exc:
            blocked[seat.seat_id] = exc
    if blocked:
        if len(seats) == 1:
            raise next(iter(blocked.values()))
        ids = ", ".join(sorted(blocked))
        raise BookingError(
            f"Not every seat is free from {_fmt_span(start_at, end_at)}: {ids} "
            f"{'is' if len(blocked) == 1 else 'are'} already booked (or blocked) in that time. "
            "Remove those seats or choose another time.",
            status=409,
            code="slot_conflict",
            extra={"seats": sorted(blocked), "taken": {k: v.extra.get("taken", []) for k, v in blocked.items()}},
        )

    now = timezone.now()
    start_h, end_h = _hour_columns(start_at, end_at)
    created = []
    batch_id = _new_batch_id()
    for seat in seats:
        fields = dict(
            seat=seat, user=user, user_name=user.profile.full_name, title=title,
            start_hour=start_h, end_hour=end_h, start_at=start_at, end_at=end_at,
        )
        if is_admin:  # an Admin does not need anybody's approval: the slot is theirs immediately
            if start_at <= now + START_TOLERANCE:
                fields.update(role=Role.ADMIN, status=Status.ACTIVE, checked_in_at=now)
            else:  # a future slot: reserved now, switches to occupied by itself when the slot starts
                fields.update(role=Role.ADMIN, status=Status.RESERVED, accepted_at=now, slot_notified=True)
        else:
            fields.update(role=Role.PROFESSOR, status=Status.PENDING, batch_id=batch_id)
        created.append(_create_reservation(**fields))

    seat_list = ", ".join(s.seat_id for s in seats)
    label = created[0].slot_label
    if is_admin:
        log_admin_action(user, Action.SEATS_RESERVED, seat_list, f"Session: {title} ({label})")
        return f"Successfully reserved {len(seats)} seats ({label})!", "success"
    notify_admins(
        f"New seat reservation request from {user.profile.full_name} for Seat{'s' if len(seats) > 1 else ''} "
        f"{seat_list} ({label}) awaiting approval.",
        reservation=created[0],
    )
    return f"Request sent for {len(seats)} seat(s) ({label}). Waiting for Admin approval.", "success"


@transaction.atomic
def cancel_reservation(user, reservation_id, seat_only=False):
    """
    A user withdraws a pending request, cancels a reservation, or ends their own session.
    For a group booking this applies to the whole group unless `seat_only` is set.
    """
    reservation = _lock_reservation(reservation_id)
    if reservation.user_id != user.id:
        raise BookingError("You can only cancel your own reservations.", status=403, code="forbidden")
    live = (Status.PENDING, *OPEN_STATUSES)
    if reservation.status not in live:
        raise BookingError("This reservation is already over.", status=409, code="already_closed")

    members = [reservation] if seat_only else [m for m in _batch_members(reservation, live) if m.user_id == user.id]
    had_pending = any(m.status == Status.PENDING for m in members)
    for member in members:
        _close(member, _user_end_reason(member))
    label, _ = _seat_list(members)
    if had_pending:
        notify_admins(f"{reservation.user_name} withdrew the request for {label}.", reservation=reservation)
        return f"Your request for {label} was cancelled.", "info"
    return f"{label} released and now available.", "info"


# ------------------------------------------------------------- admin actions
@transaction.atomic
def accept_reservation(admin, reservation_id):
    """
    Pending -> Reserved (approved). The confirmation code is created now; the 15-minute grace period starts when
    the slot's start time arrives, or immediately if the slot is already underway.
    A group booking (a professor's batch) is accepted as a whole and shares one group code.
    """
    reservation = _lock_reservation(reservation_id)
    if reservation.status != Status.PENDING:
        raise BookingError("That request is no longer pending.", status=409, code="not_pending")
    members = _batch_members(reservation, [Status.PENDING])

    now = timezone.now()
    code = _new_code()
    member_ids = [m.pk for m in members]
    live_now = False
    first_start = None
    for member in members:  # all-or-nothing: if one seat is gone, the whole group stays pending
        seat = _lock_seat(member.seat.seat_id)
        if seat.is_maintenance:
            raise BookingError(f"Seat {seat.seat_id} is under maintenance.", status=409, code="unavailable")
        if member.start_at is None or member.end_at is None:  # old request without a slot: seat must be free now
            _ensure_available(seat, ignore_pending=True)
            start = now
        else:
            if member.end_at <= now:
                raise BookingError(
                    f"The time slot {member.slot_label} for Seat {seat.seat_id} is already over. Reject this request.",
                    status=409, code="slot_over",
                )
            clash = [c for c in find_slot_conflicts(seat, member.start_at, member.end_at, member_ids)]
            accepted_clash = [c for c in clash if c.status in OPEN_STATUSES]
            if accepted_clash:
                raise _slot_conflict_error(seat, member.start_at, member.end_at, accepted_clash)
            # a queued request that overlaps the one being accepted can no longer be satisfied
            for other in clash:
                if other.status == Status.PENDING:
                    _reject(other, f"Seat {seat.seat_id} was reserved by someone else in that time.", EndReason.REJECTED)
            start = member.start_at
        starts_now = start <= now  # the slot is already underway: the check-in window opens immediately
        start = max(start, now)
        member.status = Status.RESERVED
        member.accepted_at = now
        member.grace_period_expires_at = start + GRACE_PERIOD
        member.confirmation_code = code
        member.slot_notified = starts_now
        member.save(
            update_fields=["status", "accepted_at", "grace_period_expires_at", "confirmation_code", "slot_notified"]
        )
        live_now = live_now or starts_now
        first_start = start if first_start is None else min(first_start, start)

    label, group = _seat_list(members)
    slot = members[0].slot_label
    grace_end = first_start + GRACE_PERIOD
    if live_now:
        timing = "Please check in within 15 minutes."
    else:
        timing = (
            f"Your slot starts at {_hm(first_start)}: check in with the admin between {_hm(first_start)} "
            f"and {_hm(grace_end)}."
        )
    notify(
        reservation.user,
        f"Your {'group ' if group else ''}reservation for {label}{f' ({slot})' if slot else ''} has been accepted! "
        f"Your {'group ' if group else ''}confirmation code is {code}. {timing}",
        "success",
        reservation,
    )
    log_admin_action(
        admin, Action.RESERVATION_ACCEPTED, f"{reservation.user_name} - {label}",
        f"Code {code}; {slot + '; ' if slot else ''}check in before {_hm(grace_end)}",
        target_user=reservation.user,
    )
    if live_now:
        return f"Accepted: {label} reserved for {reservation.user_name} (15 minutes to check in).", "success"
    return f"Accepted: {label} approved for {reservation.user_name} ({slot}). Queued until the slot starts.", "success"


def _reject(reservation, message, reason, notify_user=True):
    reservation.status = Status.REJECTED
    reservation.released_at = timezone.now()
    reservation.end_reason = reason
    reservation.save(update_fields=["status", "released_at", "end_reason"])
    if notify_user:
        notify(reservation.user, message, "warning", reservation)


@transaction.atomic
def reject_reservation(admin, reservation_id):
    reservation = _lock_reservation(reservation_id)
    if reservation.status != Status.PENDING:
        raise BookingError("That request is no longer pending.", status=409, code="not_pending")
    members = _batch_members(reservation, [Status.PENDING])
    for member in members:
        _reject(member, "", EndReason.REJECTED, notify_user=False)
    label, _ = _seat_list(members)
    notify(reservation.user, f"Your reservation request for {label} was rejected by the Admin.", "warning", reservation)
    log_admin_action(
        admin, Action.RESERVATION_REJECTED, f"{reservation.user_name} - {label}", target_user=reservation.user
    )
    return f"Request for {label} was rejected.", "info"


def verify_confirmation_code(admin, code):
    """Time-in: an Admin checks the code the user presents. Valid only inside the grace period."""
    expire_overdue_reservations()  # so a late code is reported as expired, not accepted
    return _verify_confirmation_code(admin, code)


@transaction.atomic
def _verify_confirmation_code(admin, code):
    code = re.sub(r"\s", "", str(code or "")).upper()
    if re.fullmatch(r"[A-Z0-9]{6}", code):
        code = f"LIB-{code}"
    if not code:
        raise BookingError("Enter the confirmation code.", code="invalid_input")

    matches = list(
        Reservation.objects.select_for_update(of=("self",))
        .select_related("seat", "user")
        .filter(confirmation_code=code)
        .order_by("seat__seat_id")
    )
    if not matches:
        raise BookingError(f"No reservation matches code {code}.", status=404, code="not_found")
    waiting = [r for r in matches if r.status == Status.RESERVED]
    if not waiting:
        statuses = {r.status for r in matches}
        if Status.ACTIVE in statuses:
            raise BookingError(f"{code} was already verified.", status=409, code="already_checked_in")
        if Status.EXPIRED in statuses:
            raise BookingError(f"{code} expired: the 15-minute grace period has passed.", status=409, code="expired")
        raise BookingError(f"{code} is no longer valid.", status=409, code="not_valid")

    now = timezone.now()
    not_started = [r for r in waiting if r.start_at and r.start_at > now]
    if not_started:
        first_slot = min(r.start_at for r in not_started)
        raise BookingError(
            f"Too early: this reservation starts at {_hm(first_slot)}. Check-in opens when the time slot starts.",
            status=409, code="too_early",
        )
    for reservation in waiting:  # a group code checks in every seat of the group at once
        # a walk-in (camera) session that is still on the seat gives way to the reservation
        for walkin in reservation.seat.reservations.select_for_update(of=("self",)).filter(
            status=Status.ACTIVE, start_at__isnull=True
        ):
            _close(walkin, EndReason.COMPLETED)
        reservation.status = Status.ACTIVE
        reservation.checked_in_at = now
        try:
            with transaction.atomic():
                reservation.save(update_fields=["status", "checked_in_at"])
        except IntegrityError:
            raise BookingError(
                f"Seat {reservation.seat.seat_id} is still occupied by the previous session.", status=409, code="seat_taken"
            )
    first = waiting[0]
    label, _ = _seat_list(waiting)
    notify(first.user, f"Time-in confirmed for {label}. Enjoy your session!", "success", first)
    log_admin_action(
        admin, Action.TIME_IN_VERIFIED, f"{first.user_name} - {label}", f"Code {code}", target_user=first.user
    )
    return f"Time-in confirmed: {first.user_name} is now seated at {', '.join(r.seat.seat_id for r in waiting)}.", "success"


def advance_schedule(now=None):
    """
    Move every seat's queue forward. Seat status is derived from the clock, so a seat switches to the next queued
    reservation by itself; this performs the side effects of that hand-over:

    - a pending request whose time slot ended before any Admin answered it lapses (it must not block the student)
    - a checked-in session ends automatically when its slot ends, freeing the seat for the next one
    - an Admin's own future slot becomes an occupied session when it starts
    - the user of an approved reservation is told once its slot has started and the 15-minute window is open
    """
    now = now or timezone.now()
    announced = set()

    # 1) stale pending requests
    for pk in list(Reservation.objects.filter(status=Status.PENDING, end_at__lt=now).values_list("pk", flat=True)):
        with transaction.atomic():
            r = Reservation.objects.select_for_update(of=("self",)).select_related("seat", "user").filter(
                pk=pk, status=Status.PENDING
            ).first()
            if r is None:
                continue
            r.status = Status.REJECTED
            r.released_at = now
            r.end_reason = EndReason.LAPSED
            r.save(update_fields=["status", "released_at", "end_reason"])
            key = ("lapse", r.user_id, r.batch_id or r.pk)
            if key not in announced:
                announced.add(key)
                notify(
                    r.user,
                    f"Your request for Seat {r.seat.seat_id} ({r.slot_label}) was not approved before its time "
                    "slot ended, so it lapsed.",
                    "warning",
                    r,
                )

    # 2) sessions that reached the end of their slot
    ended = Reservation.objects.filter(status=Status.ACTIVE, end_at__lt=now).exclude(role=Role.WALKIN)
    for pk in list(ended.values_list("pk", flat=True)):
        with transaction.atomic():
            r = Reservation.objects.select_for_update(of=("self",)).select_related("seat", "user").filter(
                pk=pk, status=Status.ACTIVE
            ).first()
            if r is None:
                continue
            _close(r, EndReason.COMPLETED)
            key = ("end", r.user_id, r.batch_id or r.pk)
            if key not in announced:
                announced.add(key)
                notify(r.user, f"Your session at Seat {r.seat.seat_id} ended at {_hm(r.end_at)}. Thank you!", "info", r)

    # 3) an Admin's own reserved slot starts by itself (no code to verify)
    due = Reservation.objects.filter(status=Status.RESERVED, role=Role.ADMIN, start_at__lte=now)
    for pk in list(due.values_list("pk", flat=True)):
        with transaction.atomic():
            r = Reservation.objects.select_for_update(of=("self",)).filter(pk=pk, status=Status.RESERVED).first()
            if r is None:
                continue
            if r.end_at and r.end_at <= now:
                _close(r, EndReason.COMPLETED)
                continue
            r.status = Status.ACTIVE
            r.checked_in_at = now
            try:
                with transaction.atomic():
                    r.save(update_fields=["status", "checked_in_at"])
            except IntegrityError:
                continue  # still occupied by someone else: try again on the next pass

    # 4) "your slot has started" for approved reservations
    started = Reservation.objects.filter(status=Status.RESERVED, slot_notified=False, start_at__lte=now)
    for pk in list(started.values_list("pk", flat=True)):
        with transaction.atomic():
            if not Reservation.objects.filter(pk=pk, status=Status.RESERVED, slot_notified=False).update(slot_notified=True):
                continue
            r = Reservation.objects.select_related("seat", "user").get(pk=pk)
            key = ("start", r.user_id, r.batch_id or r.pk)
            if key in announced:
                continue
            announced.add(key)
            until = _hm(r.grace_period_expires_at) if r.grace_period_expires_at else _hm(now + GRACE_PERIOD)
            notify(
                r.user,
                f"Your reserved slot for Seat {r.seat.seat_id} ({r.slot_label}) has started. "
                f"Check in with the admin before {until} or the seat is released.",
                "info",
                r,
            )


def expire_overdue_reservations():
    """
    Mark accepted reservations whose 15-minute grace period ran out as No-Show; the seat becomes
    available again. It also advances every seat's queue (see advance_schedule). There is no scheduler,
    so this runs whenever state is built (every poll).
    """
    now = timezone.now()
    advance_schedule(now)
    overdue = list(
        Reservation.objects.filter(status=Status.RESERVED, grace_period_expires_at__lt=now).values_list("pk", flat=True)
    )
    announced = set()  # (user, batch): a group expires seat by seat but is announced once
    for pk in overdue:
        with transaction.atomic():
            reservation = (
                Reservation.objects.select_for_update(of=("self",))
                .select_related("seat", "user")
                .filter(pk=pk, status=Status.RESERVED)
                .first()
            )
            if reservation is None:
                continue
            reservation.status = Status.EXPIRED
            reservation.released_at = now
            reservation.end_reason = EndReason.NO_SHOW
            reservation.save(update_fields=["status", "released_at", "end_reason"])
            seat_id = reservation.seat.seat_id
            key = (reservation.user_id, reservation.batch_id or reservation.pk)
            if key not in announced:
                announced.add(key)
                siblings = ""
                if reservation.batch_id:
                    ids = Reservation.objects.filter(
                        batch_id=reservation.batch_id, status__in=[Status.RESERVED, Status.EXPIRED]
                    ).values_list("seat__seat_id", flat=True)
                    siblings = ", ".join(sorted(ids))
                label = f"Seats {siblings}" if "," in siblings else f"Seat {siblings or seat_id}"
                notify(
                    reservation.user,
                    f"Your reservation for {label} expired: you did not check in within 15 minutes.",
                    "warning",
                    reservation,
                )
                notify_admins(
                    f"No-show: {reservation.user_name}'s reservation for {label} expired. The seat is available again.",
                    "warning",
                    reservation,
                )
            log_admin_action(
                None, Action.NO_SHOW_EXPIRED, f"{reservation.user_name} - Seat {seat_id}",
                f"Code {reservation.confirmation_code}; grace period ended "
                f"{timezone.localtime(reservation.grace_period_expires_at):%H:%M}",
                target_user=reservation.user,
            )
    return len(overdue)


@transaction.atomic
def release_seat(role, user, seat_id):
    seat = _lock_seat(seat_id)
    reservation = seat.current_reservation

    if role == "admin":
        if reservation is None and not seat.is_maintenance:
            raise BookingError(f"Seat {seat.seat_id} is already available.", status=409, code="already_available")
        if seat.is_maintenance:
            seat.is_maintenance = False
            seat.save(update_fields=["is_maintenance"])
    else:
        if reservation is None or reservation.user_id != user.id or reservation.role != role:
            raise BookingError("You can only release your own reservations.", status=403, code="forbidden")

    if reservation is not None:
        reason = EndReason.FORCE_RELEASED if role == "admin" else _user_end_reason(reservation)
        _close(reservation, reason)
        if role == "admin":
            notify(
                reservation.user,
                f"Your reservation for Seat {seat.seat_id} was released by the Admin.",
                "warning",
                reservation,
            )
    if role == "admin":
        details = f"Reservation held by {reservation.user_name}" if reservation else "Maintenance block lifted"
        log_admin_action(user, Action.SEAT_RELEASED, seat.seat_id, details)
    return f"Seat {seat.seat_id} has been released and is now available.", "info"


@transaction.atomic
def admin_release_reservation(admin, reservation_id):
    """Admin cancels one specific accepted reservation (including an upcoming one that is not on the seat yet)."""
    reservation = _lock_reservation(reservation_id)
    if reservation.status not in OPEN_STATUSES:
        raise BookingError("That reservation is not active or approved any more.", status=409, code="already_closed")
    seat = reservation.seat
    _close(reservation, EndReason.FORCE_RELEASED)
    notify(
        reservation.user,
        f"Your reservation for Seat {seat.seat_id} ({reservation.slot_label or 'no slot'}) was released by the Admin.",
        "warning",
        reservation,
    )
    log_admin_action(
        admin, Action.SEAT_RELEASED, seat.seat_id, f"Reservation held by {reservation.user_name}",
        target_user=reservation.user,
    )
    return f"Reservation of {reservation.user_name} for Seat {seat.seat_id} was released.", "info"


@transaction.atomic
def toggle_maintenance(user, seat_id):
    seat = _lock_seat(seat_id)
    if seat.is_maintenance:
        log_admin_action(user, Action.MAINTENANCE_OFF, seat.seat_id)
        seat.is_maintenance = False
        message = f"Seat {seat.seat_id} is back in service."
    else:
        # blocking a seat cancels everything queued on it: the live session, upcoming slots and pending requests
        open_reservations = list(seat.reservations.select_for_update(of=("self",)).filter(status__in=OPEN_STATUSES))
        for reservation in open_reservations:
            _close(reservation, EndReason.BLOCKED)
            notify(
                reservation.user,
                f"Your reservation for Seat {seat.seat_id} was cancelled: the seat is blocked for maintenance.",
                "warning",
                reservation,
            )
        for pending in seat.reservations.select_for_update(of=("self",)).filter(status=Status.PENDING):
            _reject(pending, f"Your request for Seat {seat.seat_id} was rejected: the seat is blocked.", EndReason.BLOCKED)
        names = sorted({r.user_name for r in open_reservations})
        details = f"Cancelled reservation of {', '.join(names)}" if names else ""
        log_admin_action(user, Action.MAINTENANCE_ON, seat.seat_id, details)
        seat.is_maintenance = True
        message = f"Seat {seat.seat_id} has been blocked for maintenance."
    seat.save(update_fields=["is_maintenance"])
    return message, "info"


# ------------------------------------------------------------------ camera sync
BELONGINGS_THRESHOLD = timedelta(minutes=10)
WALKIN_NAME = "Walk-in (camera)"


def _update_belongings(seat, now, person, objects):
    """Track items left with nobody present; flag the seat (once) after the threshold."""
    if person or not objects:
        seat.belongings_since, seat.belongings_flagged = None, False
        return
    if seat.belongings_since is None:
        seat.belongings_since = now
    if not seat.belongings_flagged and now - seat.belongings_since >= BELONGINGS_THRESHOLD:
        seat.belongings_flagged = True
        AdminActionLog.objects.create(
            admin=None,
            admin_username="camera",
            action=Action.BELONGINGS_FLAGGED,
            target_name=seat.seat_id,
            details=f"Items left unattended since {timezone.localtime(seat.belongings_since):%H:%M}",
        )


@transaction.atomic
def sync_seat_from_camera(seat_id, is_person_detected, is_object_detected):
    """
    Apply a *debounced* camera reading to a seat without breaking reservations.

    - Available + person            -> walk-in session (active reservation, no user)
    - Walk-in session + nobody      -> released
    - Reserved / checked-in booking -> left alone: time-in is confirmed by an Admin verifying the user's
                                       code, and the 15-minute grace period is enforced by
                                       expire_overdue_reservations()
    - Nobody but belongings         -> flagged for the admin after BELONGINGS_THRESHOLD
    Returns a short description of what changed ("" when nothing did).
    """
    expire_overdue_reservations()
    seat = _lock_seat(seat_id)
    now = timezone.now()
    seat.last_camera_check = now
    changed = ""

    if not seat.is_maintenance:
        reservation = seat.current_reservation
        if reservation is None:
            if is_person_detected:
                _create_reservation(
                    seat=seat, user=None, user_name=WALKIN_NAME, role=Role.WALKIN, status=Status.ACTIVE,
                    checked_in_at=now,
                )
                changed = "walk-in occupied"
        elif not is_person_detected and reservation.role == Role.WALKIN and reservation.status == Status.ACTIVE:
            _close(reservation, EndReason.COMPLETED)
            changed = "walk-in released"

    _update_belongings(seat, now, is_person_detected, is_object_detected)
    seat.save(update_fields=["last_camera_check", "belongings_since", "belongings_flagged"])
    return changed


# ----------------------------------------------------------- accounts & email
def _send_email(subject, body, to_email):
    """Send one email; a mail failure must never break the action that triggered it."""
    if not to_email:
        return False
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
        return True
    except Exception:
        logger.exception("Could not send email to %s", to_email)
        return False


APPROVAL_EMAIL_SUBJECT = "[LibraryOS] Your Account Has Been Approved!"
APPROVAL_EMAIL_BODY = """Dear {name},

Good day!

We are pleased to inform you that your LibraryOS account registration has been reviewed and approved by the Library Administrator. You now have full access to the Library Seat Availability and Reservation System.

Account Details:
- Name: {name}
- Role: {role}
- Registered Email / Username: {email}
- Status: Approved & Active

You can now log in to your account using the link below:
{login_url}

Important Reminders:
1. Seat Reservations: Once you submit a seat reservation request, please wait for the administrator to accept it.
2. Confirmation Code: Your unique confirmation code will appear on your dashboard once your reservation is accepted.
3. 15-Minute Grace Period: Upon approval of your reservation, please present your confirmation code to the administrator for time-in verification within 15 minutes to avoid automatic cancellation (No-Show).

Best regards,
LibraryOS Administration Team
"""


def send_account_approved_email(profile, login_url, restored=False):
    """The approval email. A restored account gets the same message: it is approved and active again."""
    body = APPROVAL_EMAIL_BODY.format(
        name=profile.full_name,
        role=profile.get_role_display(),
        email=profile.user.email,
        login_url=login_url,
    )
    return _send_email(APPROVAL_EMAIL_SUBJECT, body, profile.user.email)


def pending_account_request_count():
    return (
        Profile.objects.filter(status=Profile.Status.PENDING).count()
        + ProfileChangeRequest.objects.filter(status="pending").count()
        + PasswordResetRequest.objects.filter(status="pending").count()
    )


def notify_new_registration(profile):
    notify_admins(
        f"New {profile.get_role_display().lower()} account from {profile.full_name} awaiting approval.",
        url=approvals_url("pending"),
    )


ACCOUNT_TRANSITIONS = {
    # action: (allowed current statuses, new status, log action)
    "approve": ({Profile.Status.PENDING}, Profile.Status.APPROVED, Action.APPROVED),
    "reject": ({Profile.Status.PENDING}, Profile.Status.REJECTED, Action.REJECTED),
    "revoke": ({Profile.Status.APPROVED}, Profile.Status.REVOKED, Action.REVOKED),
    "restore": ({Profile.Status.REJECTED, Profile.Status.REVOKED}, Profile.Status.APPROVED, Action.ACCOUNT_RESTORED),
}


def set_account_status(admin, profile, action, login_url):
    """
    Approve / reject a new account, revoke an approved one, or restore an archived one.
    Approving or restoring emails the user a link to the login page. Returns (message, email_sent).
    """
    if action not in ACCOUNT_TRANSITIONS or profile.role == Profile.Role.ADMIN:
        raise BookingError("That action is not allowed for this account.", code="invalid_input")
    allowed, new_status, log_action = ACCOUNT_TRANSITIONS[action]
    if profile.status not in allowed:
        raise BookingError(f"{profile.full_name} is already {profile.get_status_display().lower()}.", status=409)

    with transaction.atomic():
        profile.status = new_status
        profile.status_changed_at = timezone.now()
        profile.status_changed_by = admin
        profile.save(update_fields=["status", "status_changed_at", "status_changed_by"])
        profile.user.is_active = new_status == Profile.Status.APPROVED
        profile.user.save(update_fields=["is_active"])
        log_admin_action(
            admin, log_action, f"{profile.full_name} ({profile.user.username})",
            f"{profile.get_role_display()} account", target_user=profile.user,
        )

    emailed = False
    if new_status == Profile.Status.APPROVED:
        emailed = send_account_approved_email(profile, login_url, restored=action == "restore")
    return f"{profile.full_name} was {log_action.label.lower()}.", emailed


def request_name_change(user, first_name, middle_name, last_name):
    """A student/professor asks for a new name. Nothing changes until an Admin approves."""
    profile = user.profile
    if profile.role == Profile.Role.ADMIN:
        raise BookingError("Admins change their name from the Django admin.", status=403, code="forbidden")
    first, middle, last = (str(v or "").strip() for v in (first_name, middle_name, last_name))
    if not first or not last:
        raise BookingError("First and last name are required.", code="invalid_input")
    if (first, middle, last) == (profile.first_name, profile.middle_name, profile.last_name):
        raise BookingError("That is already your name.", code="invalid_input")

    full = " ".join(p for p in (first, middle, last) if p)
    with transaction.atomic():
        ProfileChangeRequest.objects.filter(profile=profile, status="pending").delete()  # newest request wins
        ProfileChangeRequest.objects.create(
            profile=profile, requested_first_name=first, requested_middle_name=middle,
            requested_last_name=last, requested_full_name=full,
        )
        notify_admins(f"{profile.full_name} asked to change their name to {full}.", url=approvals_url("pending"))
    return "Name change requested. It will apply once an Admin approves it.", "success"


@transaction.atomic
def review_name_change(admin, request_id, approve):
    change = (
        ProfileChangeRequest.objects.select_for_update(of=("self",))
        .select_related("profile__user")
        .filter(pk=request_id, status="pending")
        .first()
    )
    if change is None:
        raise BookingError("That request was already handled.", status=409, code="not_pending")
    profile, user = change.profile, change.profile.user
    old_name = profile.full_name
    now = timezone.now()
    change.status = "approved" if approve else "rejected"
    change.reviewed_by, change.reviewed_at = admin, now
    change.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    if approve:
        profile.first_name = change.requested_first_name
        profile.middle_name = change.requested_middle_name
        profile.last_name = change.requested_last_name
        profile.save(update_fields=["first_name", "middle_name", "last_name"])
        user.first_name, user.last_name = profile.first_name, profile.last_name
        user.save(update_fields=["first_name", "last_name"])
        Reservation.objects.filter(user=user, status__in=[Status.PENDING, *OPEN_STATUSES]).update(
            user_name=profile.full_name
        )
        message = f"Your name was changed to {profile.full_name}."
    else:
        message = f"Your request to change your name to {change.requested_full_name} was declined."
    notify(user, message, "success" if approve else "warning")
    _send_email("Your LibraryOS name change request", f"Hello,\n\n{message}\n\nLibraryOS", user.email)
    log_admin_action(
        admin, Action.NAME_CHANGE_APPROVED if approve else Action.NAME_CHANGE_REJECTED,
        f"{old_name} -> {change.requested_full_name}", target_user=user,
    )
    return f"Name change for {old_name} {'approved' if approve else 'rejected'}.", "success" if approve else "info"


def request_password_reset(identifier, new_password):
    """
    "Forgot password": store the wanted password (hashed) for an Admin to accept. Nothing is revealed about
    whether the account exists, so this cannot be used to look up usernames or emails.
    """
    identifier = str(identifier or "").strip()
    user = (
        User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))
        .filter(is_active=True, profile__status=Profile.Status.APPROVED)
        .exclude(profile__role=Profile.Role.ADMIN)
        .first()
        if identifier
        else None
    )
    if user is not None:
        with transaction.atomic():
            PasswordResetRequest.objects.filter(user=user, status="pending").delete()
            PasswordResetRequest.objects.create(user=user, new_password_hash=make_password(new_password))
            notify_admins(
                f"{user.profile.full_name} ({user.username}) requested a password reset.", url=approvals_url("pending")
            )
    return user is not None


@transaction.atomic
def review_password_reset(admin, request_id, approve):
    reset = (
        PasswordResetRequest.objects.select_for_update(of=("self",))
        .select_related("user__profile")
        .filter(pk=request_id, status="pending")
        .first()
    )
    if reset is None:
        raise BookingError("That request was already handled.", status=409, code="not_pending")
    user = reset.user
    reset.status = "approved" if approve else "rejected"
    reset.reviewed_by, reset.reviewed_at = admin, timezone.now()
    reset.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    if approve:
        user.password = reset.new_password_hash  # already hashed when requested
        user.save(update_fields=["password"])
        message = "Your password reset request was approved. You can now sign in with the new password you chose."
    else:
        message = "Your password reset request was declined. Contact the library administrator if this is unexpected."
    notify(user, message, "success" if approve else "warning")
    _send_email("Your LibraryOS password request", f"Hello {user.profile.full_name},\n\n{message}\n\nLibraryOS", user.email)
    log_admin_action(
        admin, Action.PASSWORD_RESET_APPROVED if approve else Action.PASSWORD_RESET_REJECTED,
        f"{user.profile.full_name} ({user.username})", target_user=user,
    )
    return f"Password reset for {user.username} {'approved' if approve else 'rejected'}.", "success" if approve else "info"


# ------------------------------------------------------------ serialization
def identity(user):
    """Display details of the logged-in account, shown in the dashboard banner."""
    profile = user.profile
    return {
        "name": profile.full_name,
        "firstName": profile.first_name,
        "middleName": profile.middle_name,
        "lastName": profile.last_name,
        "roleText": profile.role_text,
        "avatar": profile.initials,
        "sub": profile.subtitle,
    }


def _iso(value):
    return value.isoformat() if value else None


def _seat_queryset():
    """Seats with their holding reservation (`open_reservations`) and waiting requests prefetched."""
    return (
        Seat.objects.select_related("zone")
        .prefetch_related(
            Prefetch(
                "reservations",
                queryset=Reservation.objects.filter(status__in=OPEN_STATUSES).order_by("start_at", "pk"),
                to_attr="open_reservations",
            ),
            Prefetch(
                "reservations",
                queryset=Reservation.objects.filter(status=Status.PENDING).order_by("created_at"),
                to_attr="pending_reservations",
            ),
        )
        .order_by("zone__code", "number")
    )


def _serialize_seat(seat, user, is_admin=False, today=None, now=None):
    now = now or timezone.now()
    reservation = seat.current_reservation
    queue = seat_day_queue(seat, today or timezone.localdate(now), user, is_admin, now)
    return {
        "id": seat.seat_id,
        "zone": seat.zone.code,
        "zoneName": seat.zone.name,
        "number": seat.number,
        "status": seat.status,
        "reservationId": reservation.pk if reservation else None,
        "user": reservation.user_name if reservation else None,
        "mine": bool(reservation and reservation.user_id == user.id),
        "role": reservation.role if reservation else None,
        "time": reservation.time_display if reservation else None,
        "features": seat.features,
        "noShow": False,
        "unattended": seat.belongings_flagged,
        # today's reservation queue (chronological): pending, approved/upcoming, active
        "queue": queue,
        "queueCount": sum(1 for q in queue if q["status"] in ("pending", "upcoming", "reserved")),
    }


def _serialize_reservation(r):
    return {
        "id": r.pk,
        "seat": r.seat.seat_id,
        "zone": r.seat.zone.code,
        "zoneName": r.seat.zone.name,
        "features": r.seat.features,
        "status": r.status,
        "role": r.role,
        "user": r.user_name,
        "title": r.title,
        "code": "" if r.status == Status.PENDING else (r.confirmation_code or ""),  # revealed only once accepted
        "batchId": r.batch_id,
        "time": r.time_display,
        "startAt": _iso(r.start_at),
        "endAt": _iso(r.end_at),
        "live": r.is_live(),
        "requestedAt": _iso(r.created_at),
        "acceptedAt": _iso(r.accepted_at),
        "graceExpiresAt": _iso(r.grace_period_expires_at),
        "checkedInAt": _iso(r.checked_in_at),
    }


def _my_reservations(user):
    queryset = (
        Reservation.objects.filter(user=user, status__in=[Status.PENDING, *OPEN_STATUSES])
        .select_related("seat__zone")
        .order_by("created_at")
    )
    return [_serialize_reservation(r) for r in queryset]


def _admin_queues():
    """Requests waiting for approval, and accepted reservations waiting for the user to check in."""
    pending = Reservation.objects.filter(status=Status.PENDING).select_related("seat__zone").order_by("created_at")
    awaiting = Reservation.objects.filter(status=Status.RESERVED).select_related("seat__zone").order_by("start_at", "grace_period_expires_at")
    awaiting_rows = []
    for r in awaiting:
        row = _serialize_reservation(r)
        row["code"] = ""  # the Admin checks the code the user presents; it is never shown back
        awaiting_rows.append(row)
    return [_serialize_reservation(r) for r in pending], awaiting_rows


def _notification_state(user):
    queryset = user.notifications.all()
    return {
        "unread": queryset.filter(read_at__isnull=True).count(),
        "items": [
            {
                "id": n.pk, "message": n.message, "kind": n.kind, "at": _iso(n.created_at),
                "read": n.read_at is not None, "url": n.target_url,
            }
            for n in queryset[:NOTIFICATIONS_SHOWN]
        ],
    }


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def build_state(role, user):
    """Everything the front end needs to render all three role views."""
    expire_overdue_reservations()
    zones = [
        {"id": z.code, "name": z.name, "title": z.title, "desc": z.description, "total": z.seat_total}
        for z in Zone.objects.annotate(seat_total=Count("seats"))
    ]
    now = timezone.now()
    today = timezone.localdate(now)
    seats = [_serialize_seat(s, user, role == "admin", today, now) for s in _seat_queryset()]

    total = len(seats)
    available = sum(1 for s in seats if s["status"] == "available")
    occupied = sum(1 for s in seats if s["status"] == "occupied")
    reserved = sum(1 for s in seats if s["status"] == "reserved")
    pending = Reservation.objects.filter(status=Status.PENDING).count()  # requests waiting in the seats' queues
    summary = {
        "total": total,
        "available": available,
        "occupied": occupied,
        "reserved": reserved,
        "pending": pending,
        "availablePct": _pct(available, total),
        "occupiedPct": _pct(occupied, total),
    }
    state = {
        "role": role,
        "users": {role: identity(user)},
        "zones": zones,
        "seats": seats,
        "summary": summary,
        "myReservations": _my_reservations(user),
        "notifications": _notification_state(user),
    }
    if role == "admin":
        state["pendingRequests"], state["awaiting"] = _admin_queues()
        state["analytics"] = get_seat_and_reservation_analytics()
        state["accountPending"] = pending_account_request_count()
    else:
        request = ProfileChangeRequest.objects.filter(profile__user=user, status="pending").first()
        state["profileRequest"] = (
            {"name": request.requested_full_name, "at": _iso(request.created_at)} if request else None
        )
    return state


# ---------------------------------------------------------------- analytics
def _pct1(part, whole):
    """Percentage rounded to 1 decimal; 0.0 when there is nothing to divide by."""
    return round(part / whole * 100, 1) if whole else 0.0


def _share(count, whole):
    return {"count": count, "pct": _pct1(count, whole)}


def _hour_counts(queryset, field):
    counts = [0] * 24
    for row in queryset.annotate(h=ExtractHour(field)).values("h").annotate(n=Count("id")):
        counts[row["h"]] = row["n"]
    return counts


def _peaks(counts, limit=3):
    ranked = sorted(((n, h) for h, n in enumerate(counts) if n), key=lambda t: (-t[0], t[1]))[:limit]
    return [{"hour": h, "label": f"{h:02d}:00-{(h + 1) % 24:02d}:00", "count": n} for n, h in ranked]


def get_seat_and_reservation_analytics():
    """
    Live occupancy figures plus reservation statistics, all percentages rounded to 1 decimal.
    Walk-in (camera) sessions count towards occupancy but not towards reservation statistics.
    """
    now = timezone.now()

    # -- live seat occupancy
    seats = list(_seat_queryset())
    total = len(seats)
    counts = {"available": 0, "pending": 0, "occupied": 0, "reserved": 0, "maintenance": 0}
    walkin = 0
    zone_counts = {}
    for seat in seats:
        status = seat.status
        counts[status] += 1
        if status == "occupied" and seat.current_reservation.role == Role.WALKIN:
            walkin += 1
        zc = zone_counts.setdefault(seat.zone, dict.fromkeys(("total", *counts), 0))
        zc["total"] += 1
        zc[status] += 1

    seat_block = {"total": total, **{k: _share(v, total) for k, v in counts.items()}}
    occupied_block = {"walkin": _share(walkin, total), "checkedIn": _share(counts["occupied"] - walkin, total)}
    zones = [
        {
            "id": zone.code,
            "name": zone.name,
            "total": zc["total"],
            "available": _share(zc["available"], zc["total"]),
            "occupied": _share(zc["occupied"], zc["total"]),
            "reserved": _share(zc["reserved"], zc["total"]),
            "utilization": _pct1(zc["occupied"] + zc["reserved"], zc["total"]),
        }
        for zone, zc in zone_counts.items()
    ]

    # -- reservations (walk-ins are not reservations)
    booked = Reservation.objects.exclude(role=Role.WALKIN)
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    agg = booked.aggregate(
        total=Count("id"),
        today=Count("id", filter=Q(created_at__gte=today_start)),
        week=Count("id", filter=Q(created_at__gte=week_start)),
        awaiting_approval=Count("id", filter=Q(status=Status.PENDING)),
        pending=Count("id", filter=Q(status=Status.RESERVED)),
        in_session=Count("id", filter=Q(status=Status.ACTIVE)),
        completed=Count("id", filter=Q(status=Status.RELEASED, checked_in_at__isnull=False)),
        cancelled=Count("id", filter=Q(status=Status.RELEASED, checked_in_at__isnull=True)),
        expired=Count("id", filter=Q(status=Status.EXPIRED)),
        rejected=Count("id", filter=Q(status=Status.REJECTED)),
    )
    r_total = agg["total"]
    reservations = {
        "total": r_total,
        "today": agg["today"],
        "week": agg["week"],
        "allTime": r_total,
        "status": {
            "awaitingApproval": _share(agg["awaiting_approval"], r_total),
            "pending": _share(agg["pending"], r_total),
            "inSession": _share(agg["in_session"], r_total),
            "completed": _share(agg["completed"], r_total),
            "cancelled": _share(agg["cancelled"], r_total),
            "expired": _share(agg["expired"], r_total),
            "rejected": _share(agg["rejected"], r_total),
        },
        # accepted and still holding a seat (waiting for check-in or in session)
        "activeRate": _pct1(agg["pending"] + agg["in_session"], r_total),
        # the user actually showed up (now or earlier)
        "fulfillmentRate": _pct1(agg["in_session"] + agg["completed"], r_total),
        # never showed up: cancelled before check-in, or expired
        "noShowRate": _pct1(agg["cancelled"] + agg["expired"], r_total),
    }

    by_role = {row["role"]: row["n"] for row in booked.values("role").annotate(n=Count("id"))}
    roles = [
        {"role": value, "label": label, **_share(by_role.get(value, 0), r_total)}
        for value, label in Reservation.Role.choices
        if value != Role.WALKIN
    ]

    # -- busiest hours and seats
    reservation_hours = _hour_counts(booked, "created_at")
    occupancy_hours = _hour_counts(Reservation.objects.filter(checked_in_at__isnull=False), "checked_in_at")

    all_sessions = Reservation.objects.count()
    top = list(
        Reservation.objects.values("seat__seat_id", "seat__zone__code")
        .annotate(n=Count("id"))
        .order_by("-n", "seat__seat_id")[:5]
    )
    minutes = {}
    for seat_id, checked_in, released in Reservation.objects.filter(
        seat__seat_id__in=[t["seat__seat_id"] for t in top], checked_in_at__isnull=False
    ).values_list("seat__seat_id", "checked_in_at", "released_at"):
        minutes[seat_id] = minutes.get(seat_id, 0) + int(((released or now) - checked_in).total_seconds() // 60)
    top_seats = [
        {
            "seat": t["seat__seat_id"],
            "zone": t["seat__zone__code"],
            "count": t["n"],
            "pct": _pct1(t["n"], all_sessions),
            "minutes": minutes.get(t["seat__seat_id"], 0),
        }
        for t in top
    ]

    return {
        "seats": seat_block,
        "occupied": occupied_block,
        "zones": zones,
        "reservations": reservations,
        "roles": roles,
        "hours": {
            "reservations": reservation_hours,
            "occupancy": occupancy_hours,
            "peakReservations": _peaks(reservation_hours),
            "peakOccupancy": _peaks(occupancy_hours),
        },
        "topSeats": top_seats,
    }


# ------------------------------------------------------- historical report
MAX_REPORT_DAYS = 366


def get_date_report(start, end=None):
    """
    Analytics for one day, or an inclusive date range (datetime.date objects, in the server time zone).

    Request figures use the reservations *requested* in the range (so the percentages share one
    denominator); occupancy uses every session that overlapped the range. The seat total is today's.
    """
    end = end or start
    if end < start:
        start, end = end, start
    if (end - start).days >= MAX_REPORT_DAYS:
        raise BookingError(f"Choose a range of at most {MAX_REPORT_DAYS} days.", code="invalid_input")

    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(start, dtime.min), tz)
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), dtime.min), tz)
    now = timezone.now()

    cancelled_q = Q(end_reason=EndReason.CANCELLED) | Q(status=Status.RELEASED, end_reason="", checked_in_at__isnull=True)
    forced_q = Q(end_reason__in=[EndReason.FORCE_RELEASED, EndReason.BLOCKED])
    requests = Reservation.objects.exclude(role=Role.WALKIN).filter(created_at__gte=lo, created_at__lt=hi)
    agg = requests.aggregate(
        total=Count("id"),
        awaiting=Count("id", filter=Q(status=Status.PENDING)),
        accepted=Count("id", filter=Q(accepted_at__isnull=False)),
        verified=Count("id", filter=Q(checked_in_at__isnull=False)),
        no_show=Count("id", filter=Q(status=Status.EXPIRED)),
        forced=Count("id", filter=forced_q),
        cancelled=Count("id", filter=cancelled_q & ~forced_q),
        rejected=Count("id", filter=Q(status=Status.REJECTED)),
    )
    total = agg["total"]
    released_or_cancelled = agg["forced"] + agg["cancelled"]

    # -- occupancy: sessions (checked in) that overlapped the range
    seat_total = Seat.objects.count()
    sessions = Reservation.objects.filter(checked_in_at__isnull=False, checked_in_at__lt=hi).filter(
        Q(released_at__isnull=True) | Q(released_at__gt=lo)
    )
    n_sessions = sessions.count()
    walkin_sessions = sessions.filter(role=Role.WALKIN).count()
    seats_used = sessions.values("seat_id").distinct().count()
    walkin_seats = sessions.filter(role=Role.WALKIN).values("seat_id").distinct().count()
    reserved_seats = sessions.exclude(role=Role.WALKIN).values("seat_id").distinct().count()

    by_role = {row["role"]: row["n"] for row in requests.values("role").annotate(n=Count("id"))}
    roles = [
        {"role": value, "label": label, **_share(by_role.get(value, 0), total)}
        for value, label in Reservation.Role.choices
        if value != Role.WALKIN
    ]

    # -- most used seats: every session started in the range, with the time they were occupied inside it
    started = Reservation.objects.filter(created_at__gte=lo, created_at__lt=hi)
    started_total = started.count()
    top = list(
        started.values("seat__seat_id", "seat__zone__code").annotate(n=Count("id")).order_by("-n", "seat__seat_id")[:5]
    )
    minutes = {}
    for seat_id, checked_in, released in sessions.filter(seat__seat_id__in=[t["seat__seat_id"] for t in top]).values_list(
        "seat__seat_id", "checked_in_at", "released_at"
    ):
        span_start, span_end = max(checked_in, lo), min(released or now, hi)
        if span_end > span_start:
            minutes[seat_id] = minutes.get(seat_id, 0) + int((span_end - span_start).total_seconds() // 60)
    top_seats = [
        {
            "seat": t["seat__seat_id"],
            "zone": t["seat__zone__code"],
            "count": t["n"],
            "pct": _pct1(t["n"], started_total),
            "minutes": minutes.get(t["seat__seat_id"], 0),
        }
        for t in top
    ]

    today = timezone.localdate()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "includesToday": start <= today <= end,
        "requests": {
            "total": total,
            "awaitingApproval": _share(agg["awaiting"], total),
            "accepted": _share(agg["accepted"], total),
            "verified": _share(agg["verified"], total),
            "noShow": _share(agg["no_show"], total),
            "forcedOrCancelled": _share(released_or_cancelled, total),
            "forceReleased": _share(agg["forced"], total),
            "cancelled": _share(agg["cancelled"], total),
            "rejected": _share(agg["rejected"], total),
        },
        "occupancy": {
            "seatTotal": seat_total,
            "seatsUsed": seats_used,
            "rate": _pct1(seats_used, seat_total),
            "sessions": n_sessions,
            "walkin": {"sessions": walkin_sessions, "seats": walkin_seats, "pct": _pct1(walkin_sessions, n_sessions),
                       "seatPct": _pct1(walkin_seats, seat_total)},
            "reserved": {"sessions": n_sessions - walkin_sessions, "seats": reserved_seats,
                         "pct": _pct1(n_sessions - walkin_sessions, n_sessions), "seatPct": _pct1(reserved_seats, seat_total)},
        },
        "roles": roles,
        "topSeats": top_seats,
    }


# ------------------------------------------------------- predictive analytics
# A small, dependency-free statistical forecaster. It learns from the reservation and occupancy history
# (hour-of-day x day-of-week patterns) and falls back on a typical-library pattern while history is thin: the
# two are blended by how many days of data exist, so the forecast improves as the system is used.
FORECAST_HISTORY_DAYS = 90      # how far back the history is read
FORECAST_MIN_DAYS = 7           # days of history at which the model is "historical" rather than "blended"
FORECAST_DAYS = 7
MIN_SESSIONS_FOR_CAPACITY = 20  # walk-in buffer / no-show headroom need this many sessions, or FORECAST_MIN_DAYS of history
HIGH_DEMAND_PCT = 70.0          # predicted occupancy at or above this is "Peak / High Demand"
MODERATE_DEMAND_PCT = 40.0      # ... at or above this (and below high) is "Moderate"
SEAT_FULL_PCT = 50.0            # a seat counts as "taken" at an hour when it is used on >= this share of days
MAX_SESSION_HOURS = 12          # one session never counts for longer than this (guards against forgotten sessions)
NO_SHOW_PRIOR_RATE = 0.15
NO_SHOW_PRIOR_WEIGHT = 10
PRIOR_WALKIN_SHARE = 0.20
PRIOR_WALKIN_WEIGHT = 5
PRIOR_STUDENT_SHARE = 0.85
PRIOR_REQUESTS_PER_SEAT = 0.4   # typical requests per day as a share of the seat count (weekday)
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Typical academic-library occupancy (% of seats) by hour of day, and a weekday multiplier (Mon..Sun).
_PRIOR_HOURLY = [2, 1, 1, 1, 1, 2, 5, 12, 25, 40, 55, 65, 60, 60, 70, 72, 68, 55, 38, 25, 15, 8, 4, 2]
_PRIOR_WEEKDAY = [1.0, 1.0, 1.0, 1.0, 0.95, 0.6, 0.35]

RISK_LABELS = {"low": "Low", "moderate": "Moderate", "peak": "Peak / High Demand"}


def _risk(pct):
    if pct >= HIGH_DEMAND_PCT:
        return "peak"
    if pct >= MODERATE_DEMAND_PCT:
        return "moderate"
    return "low"


def _hour12(hour):
    hour %= 24
    return f"{(hour % 12) or 12}:00 {'AM' if hour < 12 else 'PM'}"


def _hour_slices(start, end):
    """Split [start, end) into ((local date, hour), fraction of that hour) pieces, capped at MAX_SESSION_HOURS."""
    start, end = timezone.localtime(start), timezone.localtime(end)
    end = min(end, start + timedelta(hours=MAX_SESSION_HOURS))
    cur = start
    while cur < end:
        hour_start = cur.replace(minute=0, second=0, microsecond=0)
        seg_end = min(end, hour_start + timedelta(hours=1))
        yield (hour_start.date(), hour_start.hour), (seg_end - cur).total_seconds() / 3600
        cur = seg_end


def _smooth_rate(hits, total, prior_rate=NO_SHOW_PRIOR_RATE, prior_weight=NO_SHOW_PRIOR_WEIGHT):
    """Rate shrunk towards a prior so that a handful of records cannot swing it to 0% or 100%."""
    return (hits + prior_rate * prior_weight) / (total + prior_weight)


def _demand_windows(hours, from_hour=0):
    """Contiguous runs of high-demand hours (or, when nothing is high, the busiest run), highest first."""
    usable = [x for x in hours if x["hour"] >= from_hour]
    peak = max((x["pct"] for x in usable), default=0)
    if peak <= 0:
        return []
    level, threshold = ("high", HIGH_DEMAND_PCT) if peak >= HIGH_DEMAND_PCT else ("busiest", max(1.0, peak * 0.85))
    runs, current = [], []
    for x in usable:
        if x["pct"] >= threshold:
            current.append(x)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    windows = []
    for run in runs:
        avg = sum(x["pct"] for x in run) / len(run)
        start, end = run[0]["hour"], run[-1]["hour"] + 1
        label = f"{_hour12(start)} - {_hour12(end)}"
        lead = "High Demand Expected" if level == "high" else "Busiest window expected"
        windows.append({
            "startHour": start, "endHour": end, "label": label, "level": level,
            "avgPct": round(avg), "peakPct": round(max(x["pct"] for x in run)), "risk": _risk(avg),
            "message": f"{lead}: {label} at ~{round(avg)}% predicted occupancy",
        })
    windows.sort(key=lambda w: (-w["avgPct"], w["startHour"]))
    return windows


def get_predictive_analytics(now=None):
    """
    Forecast for the Admin dashboard (everything JSON-serialisable):
      hourly            expected occupancy % for every hour of today (+ risk badge)
      windows           predicted high-congestion windows (today's upcoming hours)
      days              the next 7 days: reservation volume (student / professor), expected utilisation, hourly
      bottlenecks       seats and zones projected to fill up earliest
      noShow            expected no-show rate and no-shows per day
      capacity          peak prediction, recommended walk-in buffer and over-booking headroom
      recommendations   actionable advice for the library admin
      meta              how much history the forecast is based on, and how it was computed
    """
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    today = local_now.date()
    since = now - timedelta(days=FORECAST_HISTORY_DAYS)
    horizon = today + timedelta(days=FORECAST_DAYS)
    seat_total = Seat.objects.count()
    seat_zone = dict(Seat.objects.values_list("seat_id", "zone__code"))
    zones = list(Zone.objects.annotate(seat_n=Count("seats")))

    # ---- occupancy log: every checked-in session (reserved check-ins and camera walk-ins)
    occ_total = defaultdict(float)                                   # (date, hour) -> seat-hours, whole library
    seat_presence = defaultdict(lambda: defaultdict(float))          # seat -> (date, hour) -> fraction used
    session_rows = list(
        Reservation.objects.filter(checked_in_at__isnull=False, checked_in_at__gte=since)
        .values_list("seat__seat_id", "role", "checked_in_at", "released_at", "end_at")
    )
    walkin_sessions = 0
    for seat_id, role, checked_in, released, end_at in session_rows:
        end = released or (min(now, end_at) if end_at else now)
        if role == Role.WALKIN:
            walkin_sessions += 1
        if end <= checked_in:
            continue
        for key, fraction in _hour_slices(checked_in, end):
            occ_total[key] += fraction
            seat_presence[seat_id][key] = max(seat_presence[seat_id][key], fraction)

    # ---- booking history: accepted reservations (a no-show is still demand for that seat)
    for seat_id, start_at, end_at in (
        Reservation.objects.exclude(role=Role.WALKIN)
        .filter(accepted_at__isnull=False, start_at__gte=since, start_at__lt=now, end_at__isnull=False)
        .values_list("seat__seat_id", "start_at", "end_at")
    ):
        for key, fraction in _hour_slices(start_at, end_at):
            seat_presence[seat_id][key] = max(seat_presence[seat_id][key], fraction)

    # ---- request volume per day and role (by the date of the booked slot)
    volume = defaultdict(lambda: defaultdict(int))                   # date -> role -> requests
    for role, start_at, created_at in (
        Reservation.objects.exclude(role=Role.WALKIN).filter(created_at__gte=since)
        .values_list("role", "start_at", "created_at")
    ):
        volume[timezone.localtime(start_at or created_at).date()][role] += 1

    history_dates = {d for (d, _h) in occ_total if d < today} | {d for d in volume if d < today}
    n_days = len(history_dates)
    first_date = min(history_dates) if history_dates else None
    dates_by_wd = defaultdict(set)
    for d in history_dates:
        dates_by_wd[d.weekday()].add(d)

    def mean_pct(dates, hour):
        if not dates or not seat_total:
            return 0.0
        return sum(occ_total.get((d, hour), 0.0) for d in dates) / len(dates) / seat_total * 100

    overall_mean = [mean_pct(history_dates, h) for h in range(24)]
    weekday_mean = {wd: [mean_pct(ds, h) for h in range(24)] for wd, ds in dates_by_wd.items()}
    w_hist = n_days / (n_days + 5.0) if n_days else 0.0

    def model_pct(wd, hour):
        prior = _PRIOR_HOURLY[hour] * _PRIOR_WEEKDAY[wd]
        if not n_days:
            return prior
        n_wd = len(dates_by_wd.get(wd, ()))
        if n_wd:
            w_wd = n_wd / (n_wd + 2.0)
            hist = w_wd * weekday_mean[wd][hour] + (1 - w_wd) * overall_mean[hour]
        else:
            hist = overall_mean[hour]
        return w_hist * hist + (1 - w_hist) * prior

    # ---- what is already booked (pending or accepted) for the coming days
    booked_pct = defaultdict(float)
    booked_count = defaultdict(lambda: defaultdict(int))             # date -> role -> reservations
    for role, start_at, end_at in (
        Reservation.objects.exclude(role=Role.WALKIN)
        .filter(status__in=BLOCKING_STATUSES, end_at__gt=now, start_at__isnull=False)
        .values_list("role", "start_at", "end_at")
    ):
        booked_count[timezone.localtime(start_at).date()][role] += 1
        for key, fraction in _hour_slices(max(start_at, now), end_at):
            if seat_total:
                booked_pct[key] += fraction / seat_total * 100

    def hourly_for(day):
        wd = day.weekday()
        rows = []
        for hour in range(24):
            model = model_pct(wd, hour)
            booked = booked_pct.get((day, hour), 0.0)
            pct = min(100.0, max(model, booked))
            rows.append({
                "hour": hour, "label": f"{hour:02d}:00", "label12": _hour12(hour),
                "pct": round(pct, 1), "modelPct": round(model, 1), "bookedPct": round(booked, 1),
                "risk": _risk(pct), "riskLabel": RISK_LABELS[_risk(pct)],
                "past": day == today and hour < local_now.hour,
            })
        return rows

    # ---- expected requests per day (student / professor)
    occurrences = defaultdict(int)
    if first_date:
        d = first_date
        while d < today:
            occurrences[d.weekday()] += 1
            d += timedelta(days=1)
    span_days = max(1, (today - first_date).days) if first_date else 1
    role_total_wd = defaultdict(lambda: defaultdict(int))
    role_total_all = defaultdict(int)
    for d, by_role in volume.items():
        if d < today:
            for role, n in by_role.items():
                role_total_wd[d.weekday()][role] += n
                role_total_all[role] += n

    def expected_requests(wd, role, prior_share):
        prior_total = seat_total * PRIOR_REQUESTS_PER_SEAT * _PRIOR_WEEKDAY[wd]
        prior = prior_total * prior_share
        if not n_days:
            return prior
        if occurrences[wd]:
            hist = role_total_wd[wd][role] / occurrences[wd]
        else:
            hist = role_total_all[role] / span_days
        return w_hist * hist + (1 - w_hist) * prior

    # ---- no-show rate from the 15-minute grace-period history
    agg = Reservation.objects.exclude(role=Role.WALKIN).filter(created_at__gte=since).aggregate(
        showed=Count("id", filter=Q(checked_in_at__isnull=False)),
        no_show=Count("id", filter=Q(status=Status.EXPIRED)),
        s_showed=Count("id", filter=Q(checked_in_at__isnull=False, role=Role.STUDENT)),
        s_no=Count("id", filter=Q(status=Status.EXPIRED, role=Role.STUDENT)),
        p_showed=Count("id", filter=Q(checked_in_at__isnull=False, role=Role.PROFESSOR)),
        p_no=Count("id", filter=Q(status=Status.EXPIRED, role=Role.PROFESSOR)),
    )
    resolved = agg["showed"] + agg["no_show"]
    no_show_rate = _smooth_rate(agg["no_show"], resolved)
    role_rates = {
        "student": _smooth_rate(agg["s_no"], agg["s_showed"] + agg["s_no"]),
        "professor": _smooth_rate(agg["p_no"], agg["p_showed"] + agg["p_no"]),
    }

    # ---- the next seven days
    days, hourly_today = [], []
    for i in range(FORECAST_DAYS):
        day = today + timedelta(days=i)
        wd = day.weekday()
        hours = hourly_for(day)
        if i == 0:
            hourly_today = hours
        student = max(round(expected_requests(wd, "student", PRIOR_STUDENT_SHARE)), booked_count[day]["student"])
        prof = max(round(expected_requests(wd, "professor", 1 - PRIOR_STUDENT_SHARE)), booked_count[day]["professor"])
        active = [x["pct"] for x in hours if x["modelPct"] >= 5 or x["bookedPct"] > 0]
        peak_row = max(hours, key=lambda x: (x["pct"], -x["hour"]))
        avg_util = sum(active) / len(active) if active else 0.0
        windows = _demand_windows(hours, local_now.hour if i == 0 else 0)
        total = student + prof
        days.append({
            "date": day.isoformat(),
            "weekday": WEEKDAY_NAMES[wd],
            "short": WEEKDAY_NAMES[wd][:3],
            "isToday": i == 0,
            "reservations": total,
            "student": student,
            "professor": prof,
            "bookedSoFar": sum(booked_count[day].values()),
            "avgUtilization": round(avg_util, 1),
            "peakUtilization": round(peak_row["pct"], 1),
            "peakHour": peak_row["hour"],
            "peakHourLabel": _hour12(peak_row["hour"]),
            "risk": _risk(peak_row["pct"]),
            "riskLabel": RISK_LABELS[_risk(peak_row["pct"])],
            "expectedNoShows": round(total * no_show_rate, 1),
            "windows": windows[:3],
            "hourly": hours,
        })

    today_windows = _demand_windows(hourly_today, local_now.hour)
    if not today_windows:  # late in the evening: look at tomorrow instead
        today_windows = days[1]["windows"] if len(days) > 1 else []
    busiest_day = max(days, key=lambda x: (x["peakUtilization"], -days.index(x)))

    # ---- bottleneck seats and zones (recurring use per hour of day)
    seat_rows, zone_rows = [], []
    if n_days:
        per_seat_hours = {}
        for seat_id, by_key in seat_presence.items():
            p = [0.0] * 24
            used_days = set()
            for (d, hour), fraction in by_key.items():
                if d < today:
                    p[hour] += min(1.0, fraction)
                    used_days.add(d)
            if used_days:
                per_seat_hours[seat_id] = ([x / n_days for x in p], len(used_days))
        for seat_id, (p, used) in per_seat_hours.items():
            peak_hour = max(range(24), key=lambda h: (p[h], -h))
            full = next((h for h in range(24) if p[h] * 100 >= SEAT_FULL_PCT), None)
            seat_rows.append({
                "seat": seat_id, "zone": seat_zone.get(seat_id, ""),
                "frequency": round(used / n_days * 100, 1),
                "peakHour": peak_hour, "peakHourLabel": _hour12(peak_hour),
                "peakProbability": round(p[peak_hour] * 100, 1),
                "projectedFullHour": full,
                "projectedFullLabel": _hour12(full) if full is not None else None,
            })
        seat_rows.sort(key=lambda r: (r["projectedFullHour"] is None, r["projectedFullHour"] or 0,
                                      -r["frequency"], r["seat"]))
        seat_rows = seat_rows[:5]
        for zone in zones:
            seats_in_zone = [sid for sid, z in seat_zone.items() if z == zone.code]
            if not seats_in_zone:
                continue
            p_zone = [
                sum(per_seat_hours.get(sid, ([0.0] * 24, 0))[0][h] for sid in seats_in_zone) / len(seats_in_zone) * 100
                for h in range(24)
            ]
            peak_hour = max(range(24), key=lambda h: (p_zone[h], -h))
            full = next((h for h in range(24) if p_zone[h] >= HIGH_DEMAND_PCT), None)
            zone_rows.append({
                "zone": zone.code, "name": zone.name, "seats": len(seats_in_zone),
                "peakPct": round(p_zone[peak_hour], 1), "peakHour": peak_hour, "peakHourLabel": _hour12(peak_hour),
                "projectedFullHour": full, "projectedFullLabel": _hour12(full) if full is not None else None,
                "risk": _risk(p_zone[peak_hour]), "riskLabel": RISK_LABELS[_risk(p_zone[peak_hour])],
            })
        zone_rows.sort(key=lambda r: (r["projectedFullHour"] is None, r["projectedFullHour"] or 0, -r["peakPct"]))

    # ---- capacity & no-show recommendations
    walkin_share = _smooth_rate(walkin_sessions, len(session_rows), PRIOR_WALKIN_SHARE, PRIOR_WALKIN_WEIGHT)
    peak_pct = busiest_day["peakUtilization"]
    # The walk-in buffer and the no-show headroom are only worth showing once there is enough real data behind them.
    capacity_ready = n_days >= FORECAST_MIN_DAYS or len(session_rows) >= MIN_SESSIONS_FOR_CAPACITY
    buffer = headroom = None
    if capacity_ready:
        buffer = max(1, math.ceil(seat_total * peak_pct / 100 * walkin_share)) if seat_total else 0
        reserved_at_peak = seat_total * peak_pct / 100 * (1 - walkin_share)
        headroom = int(reserved_at_peak * no_show_rate)
    week_reservations = sum(d["reservations"] for d in days)
    week_no_shows = round(week_reservations * no_show_rate, 1)
    top_window = today_windows[0] if today_windows else (busiest_day["windows"][0] if busiest_day["windows"] else None)

    recommendations = []
    if top_window:
        recommendations.append({
            "level": "critical" if top_window["level"] == "high" else "info",
            "title": "Peak window",
            "text": f"{top_window['message']}. Expect the library to be close to full then; remind students to "
                    "book ahead and release seats they no longer need.",
        })
    if not capacity_ready:
        recommendations.append({
            "level": "info",
            "title": "Walk-in buffer & no-show headroom",
            "text": f"Not enough data yet. These need at least {MIN_SESSIONS_FOR_CAPACITY} sessions or "
                    f"{FORECAST_MIN_DAYS} days of history (so far: {len(session_rows)} session"
                    f"{'s' if len(session_rows) != 1 else ''}, {n_days} day{'s' if n_days != 1 else ''}).",
        })
    if buffer:
        recommendations.append({
            "level": "info",
            "title": "Walk-in buffer",
            "text": f"Keep about {buffer} seat{'s' if buffer != 1 else ''} unreserved for walk-ins around the peak "
                    f"(walk-ins are ~{round(walkin_share * 100)}% of sessions).",
        })
    if headroom:
        recommendations.append({
            "level": "info",
            "title": "No-show headroom",
            "text": f"At a ~{round(no_show_rate * 100)}% no-show rate, about {headroom} reserved seat"
                    f"{'s' if headroom != 1 else ''} at the peak will probably go unused. They are released "
                    f"automatically after the 15-minute grace period, so approving up to {headroom} extra "
                    "request(s) for that window is low risk.",
        })
    if no_show_rate >= 0.30:
        recommendations.append({
            "level": "warning",
            "title": "High no-show rate",
            "text": f"{round(no_show_rate * 100)}% of accepted reservations expire unused. Consider reminding "
                    "users shortly before their slot starts or asking them to cancel early.",
        })
    if zone_rows and zone_rows[0]["projectedFullHour"] is not None:
        hot = zone_rows[0]
        calm = min(zone_rows, key=lambda r: r["peakPct"])
        advice = f" Steer new requests to {calm['name']}." if calm["zone"] != hot["zone"] else ""
        recommendations.append({
            "level": "warning",
            "title": "Bottleneck zone",
            "text": f"{hot['name']} is projected to fill up from about {hot['projectedFullLabel']} "
                    f"(peak ~{round(hot['peakPct'])}%).{advice}",
        })
    if n_days < FORECAST_MIN_DAYS:
        recommendations.append({
            "level": "info",
            "title": "Limited history",
            "text": f"Only {n_days} day{'s' if n_days != 1 else ''} of reservation history so far. The forecast "
                    "leans on a typical library pattern and becomes more accurate as data builds up.",
        })

    mode = "typical-pattern" if not n_days else ("blended" if n_days < FORECAST_MIN_DAYS else "historical")
    confidence = "Low" if n_days < 3 else ("Medium" if n_days < 14 else "High")
    return {
        "generatedAt": now.isoformat(),
        "today": today.isoformat(),
        "hourly": hourly_today,
        "windows": today_windows[:3],
        "days": days,
        "bottlenecks": {"seats": seat_rows, "zones": zone_rows},
        "noShow": {
            "rate": round(no_show_rate * 100, 1),
            "byRole": {k: round(v * 100, 1) for k, v in role_rates.items()},
            "expiredCount": agg["no_show"],
            "showedCount": agg["showed"],
            "perDay": round(week_no_shows / FORECAST_DAYS, 1),
            "nextSevenDays": week_no_shows,
        },
        "capacity": {
            "seatTotal": seat_total,
            "peakPct": peak_pct,
            "peakDay": busiest_day["weekday"],
            "peakWindow": top_window["label"] if top_window else None,
            "walkInShare": round(walkin_share * 100, 1),
            "ready": capacity_ready,
            "sessionsNeeded": max(0, MIN_SESSIONS_FOR_CAPACITY - len(session_rows)),
            "recommendedWalkInBuffer": buffer,
            "overbookHeadroom": headroom,
            "expectedReservationsWeek": week_reservations,
        },
        "recommendations": recommendations,
        "meta": {
            "historyDays": n_days,
            "sessionsAnalysed": len(session_rows),
            "mode": mode,
            "confidence": confidence,
            "note": {
                "typical-pattern": "No history yet: showing a typical library demand pattern (plus anything already booked).",
                "blended": f"Based on {n_days} day(s) of history blended with a typical pattern.",
                "historical": f"Based on {n_days} days of reservation and occupancy history.",
            }[mode],
        },
    }
