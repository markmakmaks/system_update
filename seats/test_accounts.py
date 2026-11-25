import json
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase
from django.utils import timezone

from seats import services as sv
from seats.models import AdminActionLog, Notification, PasswordResetRequest, Profile, Reservation, Seat, Zone
from seats.tests import make_user


class GroupBookingTests(TestCase):
    def setUp(self):
        zone = Zone.objects.create(code="Zone A", title="Q")
        for n in (1, 2, 3):
            Seat.objects.create(zone=zone, number=n)
        self.admin = make_user("adm", "admin")
        self.prof = make_user("prof", "professor")

    def book_group(self):
        sv.batch_reserve_for_professor(self.prof, ["A01", "A02"], "CS101", 2, timezone.localtime().hour)
        return list(Reservation.objects.filter(user=self.prof, status="pending").order_by("seat__seat_id"))

    def test_group_shares_one_code_created_on_accept(self):
        a, b = self.book_group()
        self.assertTrue(a.batch_id and a.batch_id == b.batch_id)
        self.assertIsNone(a.confirmation_code)
        sv.accept_reservation(self.admin, a.pk)  # accepting one seat accepts the whole group
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), ("reserved", "reserved"))
        self.assertTrue(a.confirmation_code.startswith("LIB-"))
        self.assertEqual(a.confirmation_code, b.confirmation_code)
        self.assertIn("A01, A02", Notification.objects.filter(user=self.prof).first().message)

        sv.verify_confirmation_code(self.admin, a.confirmation_code)  # one code checks in every seat
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), ("active", "active"))

    def test_state_hides_code_while_pending(self):
        self.book_group()
        mine = sv.build_state("professor", self.prof)["myReservations"]
        self.assertEqual([r["code"] for r in mine], ["", ""])
        self.assertEqual(len({r["batchId"] for r in mine}), 1)

    def test_group_reject(self):
        a, b = self.book_group()
        sv.reject_reservation(self.admin, b.pk)
        self.assertEqual(Reservation.objects.filter(batch_id=a.batch_id, status="rejected").count(), 2)

    def test_group_cancel_single_seat_and_whole_group(self):
        a, b = self.book_group()
        sv.accept_reservation(self.admin, a.pk)
        sv.cancel_reservation(self.prof, a.pk, seat_only=True)  # release a single seat
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), ("released", "reserved"))
        sv.cancel_reservation(self.prof, b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, "released")

    def test_group_expiry_announced_once(self):
        a, b = self.book_group()
        sv.accept_reservation(self.admin, a.pk)
        Reservation.objects.filter(batch_id=a.batch_id).update(grace_period_expires_at=timezone.now() - timedelta(seconds=1))
        sv.expire_overdue_reservations()
        self.assertEqual(Notification.objects.filter(user=self.prof, message__contains="expired").count(), 1)
        self.assertEqual(Reservation.objects.filter(batch_id=a.batch_id, status="expired").count(), 2)

    def test_notification_targets(self):
        self.book_group()
        self.assertEqual(Notification.objects.get(user=self.admin).target_url, "/admin-dashboard/#requests")
        sv.accept_reservation(self.admin, Reservation.objects.filter(user=self.prof).first().pk)
        self.assertEqual(Notification.objects.get(user=self.prof).target_url, "/professor/#sessions")
        items = sv.build_state("professor", self.prof)["notifications"]["items"]
        self.assertEqual(items[0]["url"], "/professor/#sessions")


class AccountFlowTests(TestCase):
    def setUp(self):
        self.admin = make_user("adm", "admin")
        user = User.objects.create_user("newbie", email="new@example.com", password="Sup3r-secret", is_active=False)
        self.profile = Profile.objects.create(
            user=user, role="student", status="pending", first_name="New", last_name="Bie", id_number="1"
        )
        self.client = Client()
        self.client.login(username="adm", password="x")

    def post(self, **data):
        return self.client.post("/admin-dashboard/approvals/", data, follow=True)

    def test_approve_emails_login_link_then_revoke_and_restore(self):
        resp = self.post(kind="account", profile_id=self.profile.pk, action="approve", tab="pending")
        self.assertContains(resp, "An email was sent to new@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])
        message = mail.outbox[0]
        self.assertEqual(message.subject, "[LibraryOS] Your Account Has Been Approved!")
        self.assertTrue(message.body.startswith("Dear New Bie,\n\nGood day!\n\nWe are pleased to inform you"))
        for line in ("- Name: New Bie", "- Role: Student", "- Registered Email / Username: new@example.com",
                     "- Status: Approved & Active", "http://testserver/login/",
                     "3. 15-Minute Grace Period:", "Best regards,\nLibraryOS Administration Team"):
            self.assertIn(line, message.body)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "approved")
        self.assertIsNotNone(self.profile.status_changed_at)
        self.assertTrue(Client().login(username="newbie", password="Sup3r-secret"))

        self.post(kind="account", profile_id=self.profile.pk, action="revoke", tab="approved")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "revoked")
        self.assertFalse(Client().login(username="newbie", password="Sup3r-secret"))
        self.assertContains(self.client.get("/admin-dashboard/approvals/?tab=archived"), "Restore")

        self.post(kind="account", profile_id=self.profile.pk, action="restore", tab="archived")
        self.assertEqual(len(mail.outbox), 2)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "approved")
        actions = list(AdminActionLog.objects.order_by("id").values_list("action", flat=True))
        self.assertEqual(actions, ["approved", "revoked", "account_restored"])

    def test_reject_does_not_email(self):
        self.post(kind="account", profile_id=self.profile.pk, action="reject", tab="pending")
        self.assertEqual(len(mail.outbox), 0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "rejected")

    def test_email_failure_does_not_block_approval(self):
        from unittest import mock

        with mock.patch("seats.services.send_mail", side_effect=OSError("smtp down")):
            resp = self.post(kind="account", profile_id=self.profile.pk, action="approve", tab="pending")
        self.assertContains(resp, "could not be sent")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "approved")

    def test_name_change_needs_admin_approval(self):
        student = make_user("stu", "student")
        sv.request_name_change(student, "Stu", "", "Newname")
        student.profile.refresh_from_db()
        self.assertEqual(student.profile.last_name, "T")  # unchanged until approved
        req = student.profile.change_requests.get()
        self.assertEqual((req.status, req.requested_full_name), ("pending", "Stu Newname"))
        self.assertContains(self.client.get("/admin-dashboard/approvals/"), "Stu Newname")

        self.post(kind="name", request_id=req.pk, action="approve", tab="pending")
        student.profile.refresh_from_db()
        student.refresh_from_db()
        self.assertEqual(student.profile.full_name, "Stu Newname")
        self.assertEqual(student.last_name, "Newname")
        self.assertTrue(Notification.objects.filter(user=student, message__contains="Stu Newname").exists())

        with self.assertRaises(sv.BookingError):
            sv.request_name_change(student, "Stu", "", "Newname")  # same as current

    def test_password_reset_flow(self):
        student = make_user("stu", "student")
        student.email = "stu@example.com"
        student.save()
        anon = Client()
        good = {"identifier": "STU@example.com", "password1": "Brand-new-pw-77", "password2": "Brand-new-pw-77"}
        self.assertContains(anon.post("/forgot-password/", good, follow=True), "Request received")
        reset = PasswordResetRequest.objects.get()
        self.assertNotIn("Brand-new", reset.new_password_hash)  # stored hashed
        self.assertFalse(Client().login(username="stu", password="Brand-new-pw-77"))  # not before approval

        self.post(kind="password", request_id=reset.pk, action="approve", tab="pending")
        self.assertTrue(Client().login(username="stu", password="Brand-new-pw-77"))
        self.assertFalse(Client().login(username="stu", password="x"))
        self.assertTrue(any("stu@example.com" in m.to for m in mail.outbox))

        # an unknown account gets the same answer and creates nothing
        unknown = dict(good, identifier="nobody")
        self.assertContains(anon.post("/forgot-password/", unknown, follow=True), "Request received")
        self.assertEqual(PasswordResetRequest.objects.count(), 1)
        # mismatched / weak passwords are refused
        self.assertContains(anon.post("/forgot-password/", {"identifier": "stu", "password1": "123", "password2": "456"}), "do not match")

    def test_password_reset_rejected(self):
        make_user("stu2", "student")
        sv.request_password_reset("stu2", "Another-pw-9876")
        reset = PasswordResetRequest.objects.get()
        self.post(kind="password", request_id=reset.pk, action="reject", tab="pending")
        self.assertFalse(Client().login(username="stu2", password="Another-pw-9876"))
        self.assertTrue(Client().login(username="stu2", password="x"))

    def test_admins_cannot_use_forgot_password(self):
        self.assertFalse(sv.request_password_reset("adm", "Another-pw-9876"))

    def test_date_filters(self):
        today = timezone.localdate()
        old = today - timedelta(days=40)
        Profile.objects.filter(pk=self.profile.pk).update(
            created_at=timezone.make_aware(datetime.combine(old, time(12, 0)))
        )
        page = lambda q: self.client.get(f"/admin-dashboard/approvals/?{q}").content.decode()
        self.assertIn("newbie", page("tab=pending"))
        self.assertNotIn("newbie", page(f"tab=pending&start={today}&end={today}"))
        self.assertIn("newbie", page(f"tab=pending&start={old}"))  # a single date
        self.assertNotIn("newbie", page(f"tab=pending&start={today - timedelta(days=6)}&end={today}"))
        self.assertEqual(self.client.get("/admin-dashboard/approvals/?start=2024-13-45").status_code, 200)

        sv.log_admin_action(self.admin, "approved", "someone")
        AdminActionLog.objects.update(created_at=timezone.now() - timedelta(days=10))
        logs = lambda q: self.client.get(f"/admin-dashboard/logs/?{q}").content.decode()
        self.assertIn("someone", logs("tab=admin"))
        self.assertNotIn("someone", logs(f"tab=admin&start={today}&end={today}"))
        self.assertIn("someone", logs(f"tab=admin&start={today - timedelta(days=30)}&end={today}"))
        self.assertEqual(self.client.get("/admin-dashboard/logs/?tab=seats&start=bad").status_code, 200)
        self.assertEqual(self.client.get("/admin-dashboard/users/?tab=archived&role=student").status_code, 200)

    def test_edit_profile_api(self):
        make_user("stu3", "student")
        c = Client()
        c.login(username="stu3", password="x")
        body = json.dumps({"first_name": "Stu", "middle_name": "", "last_name": "Renamed"})
        data = c.post("/api/profile/name-request/", body, content_type="application/json").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"]["profileRequest"]["name"], "Stu Renamed")
        bad = c.post("/api/profile/name-request/", json.dumps({"first_name": "", "last_name": "x"}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/api/profile/name-request/", body, content_type="application/json").status_code, 403)


class LandingPageTests(TestCase):
    def test_visitors_are_sent_to_login(self):
        c = Client()
        for url in ("/", "/student/", "/professor/", "/admin-dashboard/", "/admin-dashboard/approvals/", "/admin-dashboard/logs/"):
            resp = c.get(url)
            self.assertEqual((resp.status_code, resp["Location"]), (302, "/login/"), url)
        for url in ("/login/", "/signup/", "/forgot-password/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get("/api/live/").status_code, 401)  # the API answers with JSON, not a redirect

    def test_signed_in_users_reach_their_dashboard(self):
        make_user("stu", "student")
        c = Client()
        c.login(username="stu", password="x")
        self.assertEqual(c.get("/").url, "/student/")
        self.assertEqual(c.get("/student/").status_code, 200)
