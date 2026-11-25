from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

# A reservation that still "holds" a time slot on its seat. Pending requests are queued (they also block their
# own time slot) but do not hold the seat until an Admin accepts them; finished reservations (released, expired,
# rejected) are kept as history. A seat can have SEVERAL open reservations as long as their time slots do not
# overlap: that is the seat's queue for the day.
OPEN_STATUSES = ["reserved", "active"]


class Zone(models.Model):
    code = models.CharField(max_length=20, unique=True, help_text='e.g. "Zone A"')
    title = models.CharField(max_length=80, help_text='e.g. "Quiet Study"')
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name

    @property
    def letter(self):
        return self.code.split()[-1]

    @property
    def name(self):
        return f"{self.code} - {self.title}"


class Seat(models.Model):
    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="seats")
    number = models.PositiveSmallIntegerField()
    seat_id = models.CharField(max_length=8, unique=True, blank=True, help_text="Generated, e.g. A01")
    features = models.CharField(max_length=200, blank=True)
    is_maintenance = models.BooleanField(default=False)

    # Camera detection: region of interest in camera-frame pixels, plus bookkeeping.
    roi_x1 = models.PositiveIntegerField(null=True, blank=True)
    roi_y1 = models.PositiveIntegerField(null=True, blank=True)
    roi_x2 = models.PositiveIntegerField(null=True, blank=True)
    roi_y2 = models.PositiveIntegerField(null=True, blank=True)
    last_camera_check = models.DateTimeField(null=True, blank=True)
    belongings_since = models.DateTimeField(null=True, blank=True, help_text="Items seen with nobody present since")
    belongings_flagged = models.BooleanField(default=False, help_text="Unattended items past the threshold")

    class Meta:
        ordering = ["zone__code", "number"]
        constraints = [
            models.UniqueConstraint(fields=["zone", "number"], name="unique_seat_number_per_zone"),
        ]

    def __str__(self):
        return self.seat_id

    def save(self, *args, **kwargs):
        if not self.seat_id:
            self.seat_id = f"{self.zone.letter}{self.number:02d}"
        super().save(*args, **kwargs)

    @property
    def has_roi(self):
        coords = (self.roi_x1, self.roi_y1, self.roi_x2, self.roi_y2)
        return all(c is not None for c in coords) and self.roi_x2 > self.roi_x1 and self.roi_y2 > self.roi_y1

    @property
    def roi(self):
        return (self.roi_x1, self.roi_y1, self.roi_x2, self.roi_y2) if self.has_roi else None

    @property
    def pending_request(self):
        """The oldest request still waiting for Admin approval (or None). Uses the prefetch cache if present."""
        cached = getattr(self, "pending_reservations", None)
        if cached is not None:
            return cached[0] if cached else None
        return self.reservations.filter(status=Reservation.Status.PENDING).order_by("created_at").first()

    def _open_list(self):
        """Accepted (reserved / checked-in) reservations of this seat, earliest slot first."""
        cached = getattr(self, "open_reservations", None)
        if cached is not None:
            return sorted(cached, key=lambda r: (r.start_at is None, r.start_at or r.created_at, r.pk))
        return list(self.reservations.filter(status__in=OPEN_STATUSES).order_by("start_at", "pk"))

    @property
    def current_reservation(self):
        """
        The reservation that holds this seat *right now*, or None.

        That is the checked-in session if there is one, otherwise the accepted reservation whose slot has
        started (its 15-minute check-in window is running). Accepted reservations that start later stay
        queued (`upcoming_reservations`) and do not lock the seat.
        """
        now = timezone.now()
        open_list = self._open_list()
        for reservation in open_list:
            if reservation.status == Reservation.Status.ACTIVE:
                return reservation
        for reservation in open_list:
            if reservation.status == Reservation.Status.RESERVED and reservation.is_live(now):
                return reservation
        return None

    @property
    def upcoming_reservations(self):
        """Accepted reservations whose slot has not started yet, in chronological order."""
        now = timezone.now()
        return [
            r for r in self._open_list()
            if r.status == Reservation.Status.RESERVED and not r.is_live(now)
        ]

    @property
    def status(self):
        """available | occupied | reserved | maintenance (derived, never stored)."""
        if self.is_maintenance:
            return "maintenance"
        reservation = self.current_reservation
        if reservation is None:
            return "available"
        return "occupied" if reservation.status == Reservation.Status.ACTIVE else "reserved"


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        RESERVED = "reserved", "Reserved (awaiting check-in)"
        ACTIVE = "active", "Checked in"
        EXPIRED = "expired", "Expired (no-show)"
        REJECTED = "rejected", "Rejected"
        RELEASED = "released", "Released"

    class EndReason(models.TextChoices):
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled by user"
        FORCE_RELEASED = "force_released", "Force-released by Admin"
        BLOCKED = "blocked", "Seat blocked for maintenance"
        NO_SHOW = "no_show", "No-show (grace period expired)"
        REJECTED = "rejected", "Rejected by Admin"
        LAPSED = "lapsed", "Request lapsed (not approved before its time slot ended)"

    class Role(models.TextChoices):
        STUDENT = "student", "Student"
        PROFESSOR = "professor", "Professor"
        ADMIN = "admin", "Admin"
        WALKIN = "walkin", "Walk-in (camera)"

    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name="reservations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reservations"
    )
    user_name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices)
    title = models.CharField(max_length=100, blank=True, help_text="Session / class title (faculty batches)")
    start_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    end_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    # The booked time slot (date + start + end). Slots of one seat may not overlap while pending / accepted.
    start_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="Booked slot start")
    end_at = models.DateTimeField(null=True, blank=True, help_text="Booked slot end")
    slot_notified = models.BooleanField(default=False, help_text="The user was told their slot has started")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    confirmation_code = models.CharField(
        max_length=12, null=True, blank=True, db_index=True,
        help_text="Created when an Admin accepts the request; shown to the user, verified at time-in. "
        "Shared by every seat of a group (batch) reservation.",
    )
    batch_id = models.CharField(max_length=16, blank=True, db_index=True, help_text="Ties together a group booking")
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True, help_text="When an Admin accepted the request")
    grace_period_expires_at = models.DateTimeField(null=True, blank=True, help_text="accepted_at + 15 minutes")
    checked_in_at = models.DateTimeField(null=True, blank=True, help_text="When the code was verified / user showed up")
    released_at = models.DateTimeField(null=True, blank=True, help_text="When the reservation ended")
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Database-level guarantee that a seat never has two people checked in at once. Overlapping *future*
            # slots are prevented by services.find_slot_conflicts() while the seat row is locked.
            models.UniqueConstraint(
                fields=["seat"],
                condition=Q(status="active"),
                name="one_active_reservation_per_seat",
            ),
        ]

    def __str__(self):
        return f"{self.seat.seat_id} - {self.user_name} ({self.status})"

    def is_live(self, now=None):
        """True once the booked slot has started (a reservation without a slot counts as started)."""
        now = now or timezone.now()
        return self.start_at is None or self.start_at <= now

    @property
    def slot_label(self):
        """'14:00 - 15:30', with the date in front unless it is today (server time zone)."""
        if self.start_at is None or self.end_at is None:
            return ""
        start, end = timezone.localtime(self.start_at), timezone.localtime(self.end_at)
        label = f"{start:%H:%M} - {end:%H:%M}"
        if start.date() != timezone.localdate():
            label = f"{start:%b %d}, {label}"
        return label

    @property
    def time_display(self):
        if self.status == self.Status.EXPIRED:
            return "Expired (No-Show)"
        if self.start_at is not None and self.end_at is not None:
            label = self.slot_label
            return f"{label} ({self.title})" if self.title else label
        if self.status == self.Status.PENDING and (self.start_hour is None or self.end_hour is None):
            return "Awaiting admin approval"
        if self.start_hour is None or self.end_hour is None:
            return "Active Session"
        label = f"{self.start_hour:02d}:00 - {self.end_hour % 24:02d}:00"
        return f"{label} ({self.title})" if self.title else label


class Profile(models.Model):
    """Role and registration details for a login account. Created at sign-up, approved by an Admin."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        PROFESSOR = "professor", "Professor"
        STUDENT = "student", "Student"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        REVOKED = "revoked", "Revoked"

    class YearLevel(models.TextChoices):
        FIRST = "1st Year", "1st Year"
        SECOND = "2nd Year", "2nd Year"
        THIRD = "3rd Year", "3rd Year"
        FOURTH = "4th Year", "4th Year"
        FIFTH = "5th Year", "5th Year"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    last_name = models.CharField(max_length=80)
    first_name = models.CharField(max_length=80)
    middle_name = models.CharField(max_length=80, blank=True)
    year_level = models.CharField(max_length=20, choices=YearLevel.choices, blank=True)
    course = models.CharField(max_length=120, blank=True)
    department = models.CharField(max_length=120, blank=True)
    id_number = models.CharField(max_length=30, blank=True, help_text="Student ID or Professor ID")
    created_at = models.DateTimeField(auto_now_add=True)
    status_changed_at = models.DateTimeField(null=True, blank=True, help_text="When the status last changed")
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["role", "id_number"],
                condition=~Q(id_number=""),
                name="unique_id_number_per_role",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.role}, {self.status})"

    @property
    def full_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def initials(self):
        letters = (self.first_name[:1] + self.last_name[:1]).upper()
        return letters or self.user.username[:2].upper()

    @property
    def role_text(self):
        return {"admin": "System Admin", "professor": "Faculty Professor", "student": "Student"}[self.role]

    @property
    def subtitle(self):
        if self.role == self.Role.STUDENT:
            return f"{self.course} • {self.year_level} • Student ID: {self.id_number}"
        if self.role == self.Role.PROFESSOR:
            return f"{self.department} Department • Professor ID: {self.id_number}"
        return "Main Campus Central Library High Authority"


class AdminActionLog(models.Model):
    """Audit trail of administrative actions: who did what, to whom, and when."""

    class Action(models.TextChoices):
        APPROVED = "approved", "Approved"
        REVOKED = "revoked", "Revoked"
        REJECTED = "rejected", "Rejected"
        SEAT_RELEASED = "seat_released", "Seat released"
        MAINTENANCE_ON = "maintenance_on", "Seat blocked for maintenance"
        MAINTENANCE_OFF = "maintenance_off", "Seat back in service"
        SEATS_RESERVED = "seats_reserved", "Seats reserved"
        BELONGINGS_FLAGGED = "belongings_flagged", "Unattended belongings flagged"
        RESERVATION_ACCEPTED = "reservation_accepted", "Reservation accepted"
        RESERVATION_REJECTED = "reservation_rejected", "Reservation rejected"
        TIME_IN_VERIFIED = "time_in_verified", "Time-in code verified"
        NO_SHOW_EXPIRED = "no_show_expired", "No-show: reservation expired"
        ACCOUNT_RESTORED = "account_restored", "Account restored"
        NAME_CHANGE_APPROVED = "name_change_approved", "Name change approved"
        NAME_CHANGE_REJECTED = "name_change_rejected", "Name change rejected"
        PASSWORD_RESET_APPROVED = "password_reset_approved", "Password reset approved"
        PASSWORD_RESET_REJECTED = "password_reset_rejected", "Password reset rejected"

    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="admin_actions"
    )
    admin_username = models.CharField(max_length=150)
    action = models.CharField(max_length=30, choices=Action.choices)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    target_name = models.CharField(max_length=200, help_text="Account name or seat the action was applied to")
    details = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.admin_username} {self.action} {self.target_name}"


class Notification(models.Model):
    """In-app message for a user (Admins are notified of requests, users of decisions)."""

    class Kind(models.TextChoices):
        INFO = "info", "Info"
        SUCCESS = "success", "Success"
        WARNING = "warning", "Warning"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=300)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INFO)
    reservation = models.ForeignKey(Reservation, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    target_url = models.CharField(max_length=200, blank=True, help_text="Where clicking the notification goes")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self):
        return f"{self.user.username}: {self.message[:40]}"


class ProfileChangeRequest(models.Model):
    """A student/professor asks to change their name; it only applies once an Admin approves."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="change_requests")
    requested_first_name = models.CharField(max_length=80)
    requested_middle_name = models.CharField(max_length=80, blank=True)
    requested_last_name = models.CharField(max_length=80)
    requested_full_name = models.CharField(max_length=250)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.profile.user.username} -> {self.requested_full_name} ({self.status})"


class PasswordResetRequest(models.Model):
    """"Forgot password": the new password is stored hashed and only applied once an Admin accepts."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_requests")
    new_password_hash = models.CharField(max_length=256)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} password reset ({self.status})"
