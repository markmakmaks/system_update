from django.contrib import admin
from django.urls import reverse

from . import services
from .models import AdminActionLog, Notification, PasswordResetRequest, Profile, ProfileChangeRequest, Reservation, Seat, Zone


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "full_name", "role", "status", "id_number", "created_at")
    list_filter = ("role", "status")
    search_fields = ("user__username", "last_name", "first_name", "id_number")
    actions = ["approve_accounts", "reject_accounts"]

    @admin.action(description="Approve selected accounts")
    def approve_accounts(self, request, queryset):
        self._review(request, queryset, approved=True)

    @admin.action(description="Reject selected accounts")
    def reject_accounts(self, request, queryset):
        self._review(request, queryset, approved=False)

    def _review(self, request, queryset, approved):
        login_url = request.build_absolute_uri(reverse("seats:login"))
        for profile in queryset.select_related("user").exclude(role=Profile.Role.ADMIN):
            if approved and profile.status == Profile.Status.APPROVED:
                continue
            action = "approve" if approved else ("revoke" if profile.status == Profile.Status.APPROVED else "reject")
            if approved and profile.status != Profile.Status.PENDING:
                action = "restore"
            try:
                services.set_account_status(request.user, profile, action, login_url)
            except services.BookingError as exc:
                self.message_user(request, exc.message, level="warning")


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "description")


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ("seat_id", "zone", "number", "features", "is_maintenance")
    list_filter = ("zone", "is_maintenance")
    search_fields = ("seat_id",)
    readonly_fields = ("seat_id", "last_camera_check", "belongings_since", "belongings_flagged")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("seat", "user_name", "role", "status", "start_hour", "end_hour", "created_at")
    list_filter = ("status", "role")
    search_fields = ("user_name", "seat__seat_id", "title")


@admin.register(AdminActionLog)
class AdminActionLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "admin_username", "action", "target_name")
    list_filter = ("action",)
    readonly_fields = [f.name for f in AdminActionLog._meta.fields]


@admin.register(ProfileChangeRequest)
class ProfileChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("profile", "requested_full_name", "status", "created_at", "reviewed_by")
    list_filter = ("status",)


@admin.register(PasswordResetRequest)
class PasswordResetRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "created_at", "reviewed_by")
    list_filter = ("status",)
    exclude = ("new_password_hash",)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "message", "created_at", "read_at")
