from datetime import datetime, time, timedelta

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def backfill_slots(apps, schema_editor):
    """Give every reservation that only knew start/end *hours* a real start_at / end_at (on its request date)."""
    Reservation = apps.get_model("seats", "Reservation")
    tz = timezone.get_current_timezone()
    for r in Reservation.objects.filter(start_at__isnull=True, start_hour__isnull=False, end_hour__isnull=False):
        day = timezone.localtime(r.created_at, tz).date()
        midnight = timezone.make_aware(datetime.combine(day, time.min), tz)
        r.start_at = midnight + timedelta(hours=r.start_hour)
        r.end_at = midnight + timedelta(hours=r.end_hour)
        r.save(update_fields=["start_at", "end_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("seats", "0008_accounts_groups_notification_links"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="start_at",
            field=models.DateTimeField(blank=True, db_index=True, help_text="Booked slot start", null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="end_at",
            field=models.DateTimeField(blank=True, help_text="Booked slot end", null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="slot_notified",
            field=models.BooleanField(default=False, help_text="The user was told their slot has started"),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="end_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("completed", "Completed"),
                    ("cancelled", "Cancelled by user"),
                    ("force_released", "Force-released by Admin"),
                    ("blocked", "Seat blocked for maintenance"),
                    ("no_show", "No-show (grace period expired)"),
                    ("rejected", "Rejected by Admin"),
                    ("lapsed", "Request lapsed (not approved before its time slot ended)"),
                ],
                max_length=20,
            ),
        ),
        # several accepted reservations may now be queued on one seat (different time slots) ...
        migrations.RemoveConstraint(
            model_name="reservation",
            name="one_open_reservation_per_seat",
        ),
        # ... but only one person can be checked in at a time.
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(
                condition=Q(status="active"), fields=("seat",), name="one_active_reservation_per_seat"
            ),
        ),
        migrations.RunPython(backfill_slots, migrations.RunPython.noop),
    ]
