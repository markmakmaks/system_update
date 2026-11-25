import json
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from seats import services as sv
from seats.models import AdminActionLog, Notification, Profile, Reservation, Seat, Zone


def hour_now():
    """Start hour that makes the old duration-based API book a slot that contains 'now' (keeps tests clock-proof)."""
    return timezone.localtime().hour


def slot(day, start, end):
    return {"date": day.isoformat(), "start_time": start, "end_time": end}


def tomorrow():
    return timezone.localdate() + timedelta(days=1)


def make_user(name, role):
    user = User.objects.create_user(name, password="x")
    Profile.objects.create(user=user, role=role, status="approved", first_name=name.title(), last_name="T")
    return user


class ApprovalFlowTests(TestCase):
    def setUp(self):
        zone = Zone.objects.create(code="Zone A", title="Q")
        for n in (1, 2, 3):
            Seat.objects.create(zone=zone, number=n)
        self.admin = make_user("adm", "admin")
        self.student = make_user("stu", "student")
        self.prof = make_user("prof", "professor")

    def request(self, seat="A01"):
        sv.reserve_seat_for_student(self.student, seat, 2, hour_now())
        return Reservation.objects.get(user=self.student, status="pending")

    def accept(self, r):
        sv.accept_reservation(self.admin, r.pk)
        r.refresh_from_db()

    def test_request_is_pending_until_accepted(self):
        r = self.request()
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")  # a queued request does not lock the seat
        self.assertIsNone(r.confirmation_code)  # the code only exists once an Admin accepts
        self.assertEqual(Notification.objects.filter(user=self.admin).count(), 1)
        with self.assertRaises(sv.BookingError):  # one request at a time
            sv.reserve_seat_for_student(self.student, "A02", 1, hour_now())
        other = make_user("stu2", "student")
        with self.assertRaises(sv.BookingError) as ctx:  # the same time slot is already queued
            sv.reserve_seat_for_student(other, "A01", 1, hour_now())
        self.assertEqual(ctx.exception.code, "slot_conflict")

        self.accept(r)
        self.assertTrue(r.confirmation_code.startswith("LIB-"))
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "reserved")
        self.assertEqual(r.grace_period_expires_at - r.accepted_at, timedelta(minutes=15))
        msg = Notification.objects.filter(user=self.student).first().message
        self.assertIn(r.confirmation_code, msg)
        self.assertTrue(AdminActionLog.objects.filter(action="reservation_accepted", admin=self.admin).exists())

    def test_verify_code(self):
        r = self.request()
        with self.assertRaises(sv.BookingError):
            sv.verify_confirmation_code(self.admin, r.confirmation_code)  # not accepted yet
        self.accept(r)
        with self.assertRaises(sv.BookingError):
            sv.verify_confirmation_code(self.admin, "LIB-NOPE00")
        sv.verify_confirmation_code(self.admin, r.confirmation_code.lower().replace("lib-", ""))
        r.refresh_from_db()
        self.assertEqual(r.status, "active")
        self.assertIsNotNone(r.checked_in_at)
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "occupied")
        with self.assertRaises(sv.BookingError):
            sv.verify_confirmation_code(self.admin, r.confirmation_code)  # twice

    def test_grace_period_expiry_frees_seat(self):
        r = self.request()
        self.accept(r)
        Reservation.objects.filter(pk=r.pk).update(grace_period_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(sv.BookingError) as ctx:
            sv.verify_confirmation_code(self.admin, r.confirmation_code)  # too late
        self.assertEqual(ctx.exception.code, "expired")
        r.refresh_from_db()
        self.assertEqual((r.status, r.end_reason), ("expired", "no_show"))
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")
        self.assertTrue(AdminActionLog.objects.filter(action="no_show_expired", admin_username="system").exists())
        self.assertTrue(Notification.objects.filter(user=self.student, message__contains="expired").exists())
        sv.reserve_seat_for_student(self.student, "A01", 1, hour_now())  # free to book again

    def test_reject_force_release_and_block(self):
        r = self.request("A01")
        sv.reject_reservation(self.admin, r.pk)
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")

        r2 = self.request("A02")
        self.accept(r2)
        sv.release_seat("admin", self.admin, "A02")
        r2.refresh_from_db()
        self.assertEqual(r2.end_reason, "force_released")
        self.assertEqual(Seat.objects.get(seat_id="A02").status, "available")

        r3 = self.request("A03")
        sv.toggle_maintenance(self.admin, "A03")  # blocking rejects the waiting request
        r3.refresh_from_db()
        self.assertEqual(r3.status, "rejected")
        self.assertEqual(Seat.objects.get(seat_id="A03").status, "maintenance")

    def test_professor_batch_and_admin_batch(self):
        sv.batch_reserve_for_professor(self.prof, ["A01", "A02"], "CS", 2, hour_now())
        self.assertEqual(Reservation.objects.filter(status="pending", role="professor").count(), 2)
        sv.batch_reserve_for_professor(self.admin, ["A03"], "x", 2, hour_now())  # admins need no approval
        self.assertEqual(Seat.objects.get(seat_id="A03").status, "occupied")

    def test_camera_leaves_reservations_alone(self):
        r = self.request()
        self.accept(r)
        sv.sync_seat_from_camera("A01", True, False)
        r.refresh_from_db()
        self.assertEqual(r.status, "reserved")  # only an Admin verifying the code checks a user in
        sv.sync_seat_from_camera("A02", True, False)
        self.assertEqual(Seat.objects.get(seat_id="A02").status, "occupied")
        sv.sync_seat_from_camera("A02", False, False)
        self.assertEqual(Seat.objects.get(seat_id="A02").status, "available")

    def test_cancel_and_notifications(self):
        r = self.request()
        with self.assertRaises(sv.BookingError):
            sv.cancel_reservation(self.prof, r.pk)  # not theirs
        sv.cancel_reservation(self.student, r.pk)
        r.refresh_from_db()
        self.assertEqual(r.end_reason, "cancelled")
        sv.mark_notifications_read(self.admin)
        self.assertEqual(sv.build_state("admin", self.admin)["notifications"]["unread"], 0)

    def test_date_report_and_endpoints(self):
        r = self.request("A01")
        self.accept(r)
        sv.verify_confirmation_code(self.admin, r.confirmation_code)
        sv.sync_seat_from_camera("A03", True, False)

        today = timezone.localdate()
        rep = sv.get_date_report(today)
        self.assertEqual(rep["requests"]["total"], 1)
        self.assertEqual(rep["requests"]["accepted"], {"count": 1, "pct": 100.0})
        self.assertEqual(rep["requests"]["verified"]["pct"], 100.0)
        self.assertEqual(rep["occupancy"]["seatsUsed"], 2)
        self.assertEqual(rep["occupancy"]["rate"], 66.7)
        self.assertEqual(rep["occupancy"]["walkin"]["sessions"], 1)
        self.assertEqual(sv.get_date_report(today - timedelta(days=1))["requests"]["total"], 0)
        self.assertEqual(sv.get_date_report(date(2020, 1, 1))["occupancy"]["rate"], 0.0)

        c = Client()
        c.login(username="adm", password="x")
        data = c.get(f"/api/analytics/?start={today}&end={today}").json()
        self.assertTrue(data["ok"])
        self.assertEqual(c.get("/api/analytics/?start=bad").json()["ok"], True)  # falls back to today
        self.assertEqual(c.get("/api/analytics/?start=2024-13-45").status_code, 400)
        csv_resp = c.get(f"/admin-dashboard/report.csv?start={today}")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn(b"Checked in (code verified)", csv_resp.content)

        s = Client()
        s.login(username="stu", password="x")
        self.assertEqual(s.get("/api/analytics/").status_code, 403)
        self.assertEqual(s.get("/admin-dashboard/report.csv").status_code, 302)

    def test_http_flow(self):
        s, a = Client(), Client()
        s.login(username="stu", password="x")
        a.login(username="adm", password="x")
        post = lambda c, url, body=None: c.post(url, json.dumps(body or {}), content_type="application/json")
        self.assertTrue(post(s, "/api/seats/A01/reserve/", {"duration": 2}).json()["ok"])
        state = a.get("/api/live/").json()["state"]
        rid = state["pendingRequests"][0]["id"]
        self.assertEqual(post(s, f"/api/reservations/{rid}/accept/").status_code, 403)  # students can't accept
        self.assertTrue(post(a, f"/api/reservations/{rid}/accept/").json()["ok"])
        mine = s.get("/api/live/").json()["state"]["myReservations"][0]
        self.assertEqual(mine["status"], "reserved")
        self.assertTrue(mine["code"].startswith("LIB-"))
        self.assertEqual(a.get("/api/live/").json()["state"]["awaiting"][0]["code"], "")
        self.assertTrue(post(a, "/api/reservations/verify/", {"code": mine["code"]}).json()["ok"])
        self.assertEqual(s.get("/api/live/").json()["state"]["notifications"]["unread"], 2)
        post(s, "/api/notifications/read/")
        self.assertEqual(s.get("/api/live/").json()["state"]["notifications"]["unread"], 0)


class PageRenderTests(TestCase):
    def test_dashboards_render(self):
        zone = Zone.objects.create(code="Zone A", title="Q")
        Seat.objects.create(zone=zone, number=1)
        for name, role, url in (("adm", "admin", "/admin-dashboard/"), ("stu", "student", "/student/"),
                                ("prof", "professor", "/professor/")):
            make_user(name, role)
            c = Client()
            c.login(username=name, password="x")
            resp = c.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'id="notif-wrap"')
            self.assertNotContains(resp, "qr-checkin")
        admin = Client()
        admin.login(username="adm", password="x")
        page = admin.get("/admin-dashboard/")
        for marker in ('id="verify-code-input"', 'id="req-pending-list"', 'id="rep-start"'):
            self.assertContains(page, marker)
        self.assertContains(page, "/admin-dashboard/report.csv")
        self.assertEqual(admin.get("/admin-dashboard/logs/?tab=seats").status_code, 200)


def make_slot_reservation(seat, user, start, end, status, **extra):
    return Reservation.objects.create(
        seat=seat, user=user, user_name=user.username, role=user.profile.role, status=status,
        start_at=start, end_at=end, **extra,
    )


class TimeSlotQueueTests(TestCase):
    def setUp(self):
        zone = Zone.objects.create(code="Zone A", title="Q")
        for n in (1, 2, 3):
            Seat.objects.create(zone=zone, number=n)
        self.admin = make_user("adm", "admin")
        self.s1, self.s2, self.s3 = (make_user(f"stu{i}", "student") for i in (1, 2, 3))
        self.prof = make_user("prof", "professor")

    def test_same_seat_can_be_queued_in_different_slots(self):
        day = tomorrow()
        sv.reserve_seat_for_student(self.s1, "A01", date=day.isoformat(), start_time="10:00", end_time="11:00")
        sv.reserve_seat_for_student(self.s2, "A01", date=day.isoformat(), start_time="11:00", end_time="12:00")  # back-to-back
        self.assertEqual(Reservation.objects.filter(seat__seat_id="A01", status="pending").count(), 2)
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")

        with self.assertRaises(sv.BookingError) as ctx:  # overlaps both queued slots
            sv.reserve_seat_for_student(self.s3, "A01", date=day.isoformat(), start_time="10:30", end_time="11:30")
        self.assertEqual(ctx.exception.code, "slot_conflict")
        self.assertIn("10:00 - 11:00", ctx.exception.message)
        self.assertEqual(len(ctx.exception.extra["taken"]), 2)
        sv.reserve_seat_for_student(self.s3, "A01", date=day.isoformat(), start_time="13:00", end_time="14:00")

    def test_slot_validation(self):
        day = tomorrow().isoformat()
        for start, end in (("10:00", "10:00"), ("11:00", "10:00"), ("10:00", "10:15"), ("08:00", "12:00")):
            with self.assertRaises(sv.BookingError):
                sv.reserve_seat_for_student(self.s1, "A01", date=day, start_time=start, end_time=end)
        with self.assertRaises(sv.BookingError):  # past date
            sv.reserve_seat_for_student(self.s1, "A01", date="2020-01-01", start_time="10:00", end_time="11:00")
        with self.assertRaises(sv.BookingError):  # too far ahead
            far = (timezone.localdate() + timedelta(days=30)).isoformat()
            sv.reserve_seat_for_student(self.s1, "A01", date=far, start_time="10:00", end_time="11:00")
        with self.assertRaises(sv.BookingError):  # missing time
            sv.reserve_seat_for_student(self.s1, "A01", date=day, start_time="10:00")

    def test_future_approved_slot_does_not_lock_the_seat_and_checkin_waits(self):
        day = tomorrow()
        sv.reserve_seat_for_student(self.s1, "A01", date=day.isoformat(), start_time="10:00", end_time="11:00")
        r = Reservation.objects.get(user=self.s1)
        sv.accept_reservation(self.admin, r.pk)
        r.refresh_from_db()
        self.assertEqual(r.status, "reserved")
        self.assertEqual(r.grace_period_expires_at - r.start_at, timedelta(minutes=15))  # grace starts at the slot
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")  # not started yet
        with self.assertRaises(sv.BookingError) as ctx:
            sv.verify_confirmation_code(self.admin, r.confirmation_code)
        self.assertEqual(ctx.exception.code, "too_early")
        sv.expire_overdue_reservations()
        r.refresh_from_db()
        self.assertEqual(r.status, "reserved")  # the grace period has not started, so it cannot expire yet

    def test_queue_advances_when_the_current_slot_ends(self):
        now = timezone.now()
        seat = Seat.objects.get(seat_id="A01")
        first = make_slot_reservation(seat, self.s1, now - timedelta(minutes=30), now + timedelta(minutes=30), "reserved",
                                      grace_period_expires_at=now + timedelta(minutes=10), slot_notified=True)
        second = make_slot_reservation(seat, self.s2, now + timedelta(minutes=30), now + timedelta(minutes=90), "reserved",
                                       grace_period_expires_at=now + timedelta(minutes=45))
        seat = Seat.objects.get(seat_id="A01")
        self.assertEqual(seat.status, "reserved")
        self.assertEqual(seat.current_reservation.pk, first.pk)
        self.assertEqual([r.pk for r in seat.upcoming_reservations], [second.pk])

        # the first user is checked in and their slot ends
        Reservation.objects.filter(pk=first.pk).update(
            status="active", checked_in_at=now - timedelta(minutes=25), end_at=now - timedelta(seconds=5)
        )
        sv.expire_overdue_reservations()
        first.refresh_from_db()
        self.assertEqual((first.status, first.end_reason), ("released", "completed"))
        self.assertEqual(Seat.objects.get(seat_id="A01").status, "available")  # second slot not started yet

        # the second slot starts: the seat switches to reserved and the user is told once
        Reservation.objects.filter(pk=second.pk).update(
            start_at=now - timedelta(seconds=5), grace_period_expires_at=now + timedelta(minutes=15)
        )
        sv.expire_overdue_reservations()
        sv.expire_overdue_reservations()
        seat = Seat.objects.get(seat_id="A01")
        self.assertEqual(seat.status, "reserved")
        self.assertEqual(seat.current_reservation.pk, second.pk)
        self.assertEqual(Notification.objects.filter(user=self.s2, message__contains="has started").count(), 1)

    def test_stale_pending_request_lapses(self):
        now = timezone.now()
        seat = Seat.objects.get(seat_id="A01")
        r = make_slot_reservation(seat, self.s1, now - timedelta(hours=2), now - timedelta(hours=1), "pending")
        sv.expire_overdue_reservations()
        r.refresh_from_db()
        self.assertEqual((r.status, r.end_reason), ("rejected", "lapsed"))
        sv.reserve_seat_for_student(self.s1, "A02", 1, hour_now())  # the single-booking rule no longer blocks them

    def test_admin_can_release_an_upcoming_reservation_and_maintenance_clears_queue(self):
        day = tomorrow().isoformat()
        sv.reserve_seat_for_student(self.s1, "A01", date=day, start_time="10:00", end_time="11:00")
        sv.reserve_seat_for_student(self.s2, "A01", date=day, start_time="12:00", end_time="13:00")
        r1 = Reservation.objects.get(user=self.s1)
        sv.accept_reservation(self.admin, r1.pk)
        sv.admin_release_reservation(self.admin, r1.pk)
        r1.refresh_from_db()
        self.assertEqual(r1.end_reason, "force_released")
        sv.toggle_maintenance(self.admin, "A01")  # blocking rejects the other queued request
        self.assertEqual(Reservation.objects.get(user=self.s2).status, "rejected")

    def test_professor_batch_conflict_is_all_or_nothing(self):
        day = tomorrow().isoformat()
        sv.reserve_seat_for_student(self.s1, "A02", date=day, start_time="10:00", end_time="11:00")
        with self.assertRaises(sv.BookingError) as ctx:
            sv.batch_reserve_for_professor(self.prof, ["A01", "A02"], "CS", date=day, start_time="10:30", end_time="12:00")
        self.assertEqual(ctx.exception.code, "slot_conflict")
        self.assertEqual(Reservation.objects.filter(user=self.prof).count(), 0)
        sv.batch_reserve_for_professor(self.prof, ["A01", "A02"], "CS", date=day, start_time="11:00", end_time="12:00")
        self.assertEqual(Reservation.objects.filter(user=self.prof, status="pending").count(), 2)

    def test_admin_future_slot_starts_by_itself(self):
        day = tomorrow().isoformat()
        sv.batch_reserve_for_professor(self.admin, ["A03"], "Event", date=day, start_time="09:00", end_time="10:00")
        r = Reservation.objects.get(user=self.admin)
        self.assertEqual(r.status, "reserved")
        Reservation.objects.filter(pk=r.pk).update(
            start_at=timezone.now() - timedelta(minutes=1), end_at=timezone.now() + timedelta(hours=1)
        )
        sv.expire_overdue_reservations()
        r.refresh_from_db()
        self.assertEqual(r.status, "active")

    def test_schedule_endpoint_hides_other_names_from_non_admins(self):
        sv.reserve_seat_for_student(self.s1, "A01", date=tomorrow().isoformat(), start_time="10:00", end_time="11:00")
        day = tomorrow().isoformat()
        c = Client()
        c.login(username="stu2", password="x")
        data = c.get(f"/api/seats/A01/schedule/?date={day}").json()
        self.assertTrue(data["ok"])
        item = data["schedule"]["items"][0]
        self.assertEqual((item["span"], item["status"], item["who"]), ("10:00 - 11:00", "pending", "Student"))
        a = Client()
        a.login(username="adm", password="x")
        item = a.get(f"/api/seats/A01/schedule/?date={day}").json()["schedule"]["items"][0]
        self.assertEqual(item["who"], "Stu1 T")
        self.assertEqual(c.get("/api/seats/A01/schedule/?date=nope").status_code, 400)
        self.assertEqual(c.get("/api/seats/ZZ9/schedule/").status_code, 404)

    def test_http_booking_with_slot_and_conflict_message(self):
        c = Client()
        c.login(username="stu1", password="x")
        c2 = Client()
        c2.login(username="stu2", password="x")
        body = slot(tomorrow(), "14:00", "15:00")
        post = lambda cl, b: cl.post("/api/seats/A01/reserve/", json.dumps(b), content_type="application/json")
        self.assertTrue(post(c, body).json()["ok"])
        resp = post(c2, slot(tomorrow(), "14:30", "15:30"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "slot_conflict")
        self.assertIn("14:00 - 15:00", resp.json()["error"])


class PredictiveAnalyticsTests(TestCase):
    def setUp(self):
        zone = Zone.objects.create(code="Zone A", title="Q")
        for n in range(1, 5):
            Seat.objects.create(zone=zone, number=n)
        self.admin = make_user("adm", "admin")
        self.stu = make_user("stu", "student")

    def add_history(self, days=10):
        seat = Seat.objects.get(seat_id="A01")
        base = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
        for d in range(1, days + 1):
            start = base - timedelta(days=d)
            make_slot_reservation(
                seat, self.stu, start, start + timedelta(hours=2), "released",
                accepted_at=start, checked_in_at=start, released_at=start + timedelta(hours=2), end_reason="completed",
            )
        make_slot_reservation(
            seat, self.stu, base - timedelta(days=1, hours=-4), base - timedelta(days=1, hours=-5), "expired",
            accepted_at=base, end_reason="no_show",
        )

    def test_forecast_without_history_uses_typical_pattern(self):
        f = sv.get_predictive_analytics()
        self.assertEqual(f["meta"]["mode"], "typical-pattern")
        self.assertEqual(len(f["hourly"]), 24)
        self.assertEqual(len(f["days"]), 7)
        self.assertTrue(all(0 <= h["pct"] <= 100 for h in f["hourly"]))
        self.assertIn(f["hourly"][12]["risk"], ("low", "moderate", "peak"))
        self.assertGreater(f["noShow"]["rate"], 0)
        self.assertEqual(f["bottlenecks"]["seats"], [])
        self.assertFalse(f["capacity"]["ready"])  # not enough data: no walk-in buffer / headroom yet
        self.assertIsNone(f["capacity"]["recommendedWalkInBuffer"])
        self.assertIsNone(f["capacity"]["overbookHeadroom"])
        self.assertTrue(any("Not enough data yet" in r["text"] for r in f["recommendations"]))

    def test_forecast_learns_from_history_and_booked_slots(self):
        self.add_history(10)
        f = sv.get_predictive_analytics()
        self.assertEqual(f["meta"]["mode"], "historical")
        self.assertEqual(f["meta"]["historyDays"], 10)
        self.assertTrue(f["capacity"]["ready"])  # 10 days of history is enough
        self.assertIsNotNone(f["capacity"]["recommendedWalkInBuffer"])
        self.assertEqual(f["bottlenecks"]["seats"][0]["seat"], "A01")
        self.assertEqual(f["bottlenecks"]["seats"][0]["projectedFullHour"], 10)
        self.assertEqual((f["noShow"]["expiredCount"], f["noShow"]["showedCount"]), (1, 10))
        self.assertLess(f["noShow"]["rate"], 15)  # 1 expiry in 11 resolved reservations, shrunk towards the prior
        self.assertTrue(all(d["student"] >= 0 and d["professor"] >= 0 for d in f["days"]))

        sv.reserve_seat_for_student(self.stu, "A02", date=tomorrow().isoformat(), start_time="16:00", end_time="17:00")
        f = sv.get_predictive_analytics()
        tomorrow_hours = f["days"][1]["hourly"]
        self.assertEqual(tomorrow_hours[16]["bookedPct"], 25.0)  # 1 of 4 seats
        self.assertGreaterEqual(tomorrow_hours[16]["pct"], 25.0)
        self.assertGreaterEqual(f["days"][1]["bookedSoFar"], 1)

    def test_endpoints_and_csv(self):
        self.add_history(8)
        c = Client()
        c.login(username="adm", password="x")
        data = c.get("/api/forecast/").json()
        self.assertTrue(data["ok"])
        self.assertIn("recommendations", data["forecast"])
        resp = c.get("/admin-dashboard/forecast.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Predictive analytics (forecast)", resp.content)
        self.assertIn(b"Predictive analytics (forecast)", c.get("/admin-dashboard/analytics.csv").content)
        s = Client()
        s.login(username="stu", password="x")
        self.assertEqual(s.get("/api/forecast/").status_code, 403)
        page = c.get("/admin-dashboard/")
        self.assertContains(page, 'id="admin-panel-forecast"')
