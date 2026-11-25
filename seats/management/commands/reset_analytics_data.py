import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from seats.models import AdminActionLog, Notification, Reservation


class Command(BaseCommand):
    help = (
        "Start the analytics fresh: delete every reservation / session (the data behind Descriptive Analytics and the "
        "Predictive Analytics forecast). Seats, zones and accounts are kept."
    )

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
        parser.add_argument("--no-backup", action="store_true", help="Do not copy db.sqlite3 before deleting.")
        parser.add_argument(
            "--include-logs", action="store_true",
            help="Also clear the Admin action log (accepted / rejected / force-released history).",
        )

    def handle(self, *args, **options):
        reservations = Reservation.objects.count()
        notifications = Notification.objects.filter(reservation__isnull=False).count()
        logs = AdminActionLog.objects.count() if options["include_logs"] else 0

        self.stdout.write("This will permanently delete:")
        self.stdout.write(f"  - {reservations} reservations / sessions (history for the analytics and the forecast)")
        self.stdout.write(f"  - {notifications} notifications about those reservations")
        if options["include_logs"]:
            self.stdout.write(f"  - {logs} admin action log entries")
        self.stdout.write("Seats, zones, maintenance blocks and user accounts are NOT touched.")

        if reservations == 0 and notifications == 0 and logs == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to delete: the analytics data is already empty."))
            return

        if not options["yes"]:
            answer = input("Type 'yes' to continue: ").strip().lower()
            if answer != "yes":
                raise CommandError("Cancelled. Nothing was deleted.")

        if not options["no_backup"] and connection.vendor == "sqlite":
            db_path = Path(str(settings.DATABASES["default"]["NAME"]))
            if db_path.exists():
                backup = db_path.with_name(f"{db_path.name}.backup-{datetime.now():%Y%m%d-%H%M%S}")
                shutil.copy2(db_path, backup)
                self.stdout.write(f"Backup saved to {backup}")

        with transaction.atomic():
            Notification.objects.filter(reservation__isnull=False).delete()
            Reservation.objects.all().delete()
            if options["include_logs"]:
                AdminActionLog.objects.all().delete()

        self.stdout.write(self.style.SUCCESS(
            "Done. The analytics start fresh. The forecast uses the typical library pattern until new "
            "reservations and check-ins build up."
        ))
