import csv
import json
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import services
from .forms import ForgotPasswordForm, ProfileNameForm, SignUpForm
from .models import AdminActionLog, PasswordResetRequest, Profile, ProfileChangeRequest, Reservation
from .services import BookingError

POLL_INTERVAL_MS = 4000
SEAT_PLACEHOLDER = "__SEAT__"
ID_PLACEHOLDER = "__ID__"

DASHBOARD_URLS = {
    "admin": "seats:admin-dashboard",
    "professor": "seats:professor-dashboard",
    "student": "seats:student-dashboard",
}


def _profile(user):
    """The approved profile of a signed-in user, or None."""
    if not user.is_authenticated:
        return None
    profile = Profile.objects.filter(user=user).first()
    if profile is None and user.is_superuser:  # e.g. created with `createsuperuser`
        profile = Profile.objects.create(
            user=user,
            role=Profile.Role.ADMIN,
            status=Profile.Status.APPROVED,
            first_name=user.first_name or user.username,
            last_name=user.last_name,
        )
    if profile is None or profile.status != Profile.Status.APPROVED:
        return None
    return profile


def _require_role(role, *allowed):
    if role not in allowed:
        raise BookingError("Your current role is not allowed to do that.", status=403, code="forbidden")


def _json_body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise BookingError("Request body must be valid JSON.", code="invalid_json")
    if not isinstance(data, dict):
        raise BookingError("Request body must be a JSON object.", code="invalid_json")
    return data


def api_post(view):
    """
    POST-only, sign-in-required JSON endpoint. The wrapped view receives
    (request, role, user, ...) and returns (message, level). On success the
    response carries the fresh state so the page can re-render without a second request.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        profile = _profile(request.user)
        if profile is None:
            return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
        role, user = profile.role, request.user
        try:
            message, level = view(request, role, user, *args, **kwargs)
        except BookingError as exc:
            return JsonResponse(
                {"ok": False, "error": exc.message, "code": exc.code, **exc.extra}, status=exc.status
            )
        return JsonResponse(
            {"ok": True, "message": message, "level": level, "state": services.build_state(role, user)}
        )

    return require_POST(wrapper)


# --------------------------------------------------------------------- auth
def _home_for(profile):
    return redirect(DASHBOARD_URLS[profile.role])


def home(request):
    profile = _profile(request.user)
    return _home_for(profile) if profile else redirect("seats:login")


def login_view(request):
    profile = _profile(request.user)
    if profile:
        return _home_for(profile)

    error = None
    username = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        account = User.objects.filter(username__iexact=username).first()
        if account and not account.is_active and account.check_password(password):
            status = getattr(getattr(account, "profile", None), "status", None)
            if status == Profile.Status.REJECTED:
                error = "Your registration was not approved. Please contact the library administrator."
            elif status == Profile.Status.REVOKED:
                error = "Your access has been revoked. Please contact the library administrator."
            else:
                error = "Your account is still waiting for Admin approval. You can sign in once it is approved."
        else:
            user = authenticate(request, username=account.username if account else username, password=password)
            if user is None:
                error = "Invalid username or password."
            elif _profile(user) is None:
                error = "This account has no approved library profile."
            else:
                login(request, user)
                return _home_for(user.profile)
    return render(request, "seats/auth/login.html", {"error": error, "username": username})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("seats:login")


def signup_view(request):
    profile = _profile(request.user)
    if profile:
        return _home_for(profile)
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            services.notify_new_registration(user.profile)
            messages.success(
                request, "Registration submitted! An Admin must approve your account before you can sign in."
            )
            return redirect("seats:login")
    else:
        role = request.GET.get("role")
        form = SignUpForm(initial={"role": role if role in ("student", "professor") else "student"})
    return render(request, "seats/auth/signup.html", {"form": form})


def _admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        profile = _profile(request.user)
        if profile is None:
            return redirect("seats:login")
        if profile.role != Profile.Role.ADMIN:
            return _home_for(profile)
        return view(request, *args, **kwargs)

    return wrapper


def forgot_password_view(request):
    """Public page: ask for a new password. An Admin has to accept the request before it applies."""
    if _profile(request.user):
        return _home_for(_profile(request.user))
    if request.method == "POST":
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            services.request_password_reset(form.cleaned_data["identifier"], form.cleaned_data["password1"])
            # the same answer whether or not an account matched, so usernames/emails cannot be probed
            messages.success(
                request,
                "Request received. If the account exists, an Admin will review it and you will be emailed "
                "once your new password is active.",
            )
            return redirect("seats:login")
    else:
        form = ForgotPasswordForm()
    return render(request, "seats/auth/forgot_password.html", {"form": form})


# ------------------------------------------------- date filter + account tabs
ACCOUNT_TABS = ("pending", "approved", "archived")


def _date_filter(request, keep=()):
    """
    Reads ?start=YYYY-MM-DD&end=YYYY-MM-DD (a single day when only one is given) and builds what the
    shared date-filter widget needs: the values, quick-preset query strings and the params to carry over.
    """
    today = timezone.localdate()
    try:
        start = parse_date(request.GET.get("start") or "")
        end = parse_date(request.GET.get("end") or "")
    except ValueError:
        start = end = None
    start, end = start or end, end or start
    if start and end and end < start:
        start, end = end, start

    carried = {k: request.GET[k] for k in keep if request.GET.get(k)}
    presets = {
        "today": (today, today),
        "week": (today - timedelta(days=6), today),
        "month": (today.replace(day=1), today),
    }
    return {
        "start": start,
        "end": end,
        "start_value": start.isoformat() if start else "",
        "end_value": end.isoformat() if end else "",
        "carried": carried,
        "presets": {
            key: urlencode({**carried, "start": lo.isoformat(), "end": hi.isoformat()}) for key, (lo, hi) in presets.items()
        },
        "active_preset": next((k for k, v in presets.items() if v == (start, end)), ""),
        "clear_query": urlencode(carried),
        "is_filtered": bool(start),
    }


def _within(queryset, field, flt):
    if not flt["start"]:
        return queryset
    return queryset.filter(**{f"{field}__date__gte": flt["start"], f"{field}__date__lte": flt["end"]})


def _accounts_for_tab(tab):
    """Accounts (never Admins) in one tab, each with the date of its latest status change as `event_at`."""
    accounts = (
        Profile.objects.exclude(role=Profile.Role.ADMIN)
        .select_related("user")
        .annotate(event_at=Coalesce("status_changed_at", "created_at"))
    )
    if tab == "pending":
        return accounts.filter(status=Profile.Status.PENDING).order_by("created_at")
    if tab == "approved":
        return accounts.filter(status=Profile.Status.APPROVED).order_by("-event_at")
    return accounts.filter(status__in=[Profile.Status.REJECTED, Profile.Status.REVOKED]).order_by("-event_at")


def _tab_counts():
    return {
        "pending": (
            Profile.objects.filter(status=Profile.Status.PENDING).count()
            + ProfileChangeRequest.objects.filter(status="pending").count()
            + PasswordResetRequest.objects.filter(status="pending").count()
        ),
        "approved": Profile.objects.exclude(role=Profile.Role.ADMIN).filter(status=Profile.Status.APPROVED).count(),
        "archived": Profile.objects.exclude(role=Profile.Role.ADMIN)
        .filter(status__in=[Profile.Status.REJECTED, Profile.Status.REVOKED])
        .count(),
    }


@_admin_only
def approvals(request):
    if request.method == "POST":
        kind = request.POST.get("kind", "account")
        action = request.POST.get("action", "")
        try:
            if kind == "account":
                target = get_object_or_404(Profile.objects.select_related("user"), pk=request.POST.get("profile_id"))
                message, emailed = services.set_account_status(
                    request.user, target, action, request.build_absolute_uri(reverse("seats:login"))
                )
                if action in ("approve", "restore"):
                    if emailed:
                        message += f" An email was sent to {target.user.email}."
                    elif target.user.email:
                        message += " The notification email could not be sent (check the email settings)."
                messages.success(request, message)
            elif kind in ("name", "password"):
                review = services.review_name_change if kind == "name" else services.review_password_reset
                message, _ = review(request.user, request.POST.get("request_id"), action == "approve")
                messages.success(request, message)
            else:
                raise BookingError("Unknown request.")
        except BookingError as exc:
            messages.error(request, exc.message)
        back = {k: request.POST[k] for k in ("tab", "start", "end") if request.POST.get(k)}
        return redirect(f"{reverse('seats:approvals')}?{urlencode(back)}")

    tab = request.GET.get("tab") if request.GET.get("tab") in ACCOUNT_TABS else "pending"
    flt = _date_filter(request, keep=("tab",))
    flt["carried"] = {"tab": tab}
    flt["presets"] = {k: v if "tab=" in v else f"tab={tab}&{v}" for k, v in flt["presets"].items()}
    flt["clear_query"] = f"tab={tab}"
    context = {
        "tab": tab,
        "counts": _tab_counts(),
        "flt": flt,
        "accounts": _within(_accounts_for_tab(tab), "event_at", flt),
        "current_user": services.identity(request.user),
        "nav": "approvals",
    }
    if tab == "pending":
        context["name_requests"] = _within(
            ProfileChangeRequest.objects.filter(status="pending").select_related("profile__user").order_by("created_at"),
            "created_at", flt,
        )
        context["password_requests"] = _within(
            PasswordResetRequest.objects.filter(status="pending").select_related("user__profile").order_by("created_at"),
            "created_at", flt,
        )
    return render(request, "seats/auth/approvals.html", context)


@_admin_only
def users_list(request):
    role = request.GET.get("role")
    if role not in (Profile.Role.STUDENT, Profile.Role.PROFESSOR):
        role = "all"
    tab = request.GET.get("tab") if request.GET.get("tab") in ACCOUNT_TABS else "approved"
    flt = _date_filter(request, keep=("tab", "role"))
    flt["carried"] = {"tab": tab, "role": role}
    flt["presets"] = {k: f"tab={tab}&role={role}&{v}" if "tab=" not in v else v for k, v in flt["presets"].items()}
    flt["clear_query"] = f"tab={tab}&role={role}"
    profiles = _accounts_for_tab(tab).order_by("last_name", "first_name") if tab == "approved" else _accounts_for_tab(tab)
    if role != "all":
        profiles = profiles.filter(role=role)
    context = {
        "profiles": _within(profiles, "event_at", flt),
        "role_filter": role,
        "tab": tab,
        "counts": _tab_counts(),
        "flt": flt,
        "current_user": services.identity(request.user),
        "nav": "users",
    }
    return render(request, "seats/auth/users.html", context)


@_admin_only
def activity_logs(request):
    tab = "seats" if request.GET.get("tab") == "seats" else "admin"
    flt = _date_filter(request, keep=("tab",))
    flt["carried"] = {"tab": tab}
    flt["presets"] = {k: f"tab={tab}&{v}" if "tab=" not in v else v for k, v in flt["presets"].items()}
    flt["clear_query"] = f"tab={tab}"
    context = {"tab": tab, "flt": flt, "current_user": services.identity(request.user), "nav": "logs"}
    if tab == "seats":
        reservations = Reservation.objects.select_related("seat__zone").order_by("-created_at")
        context["reservations"] = _within(reservations, "created_at", flt)[:300]
    else:
        context["actions"] = _within(AdminActionLog.objects.all(), "created_at", flt)[:300]
    return render(request, "seats/auth/activity_logs.html", context)


@_admin_only
def analytics_csv(request):
    a = services.get_seat_and_reservation_analytics()
    response = HttpResponse(content_type="text/csv")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="libraryos-analytics-{stamp}.csv"'
    w = csv.writer(response)

    w.writerow(["LibraryOS analytics report", timezone.localtime().strftime("%Y-%m-%d %H:%M")])
    w.writerow([])
    w.writerow(["Seat occupancy", "Count", "% of total seats"])
    w.writerow(["Total seats", a["seats"]["total"], ""])
    for key, label in (("available", "Available"), ("pending", "Request pending approval"), ("occupied", "Occupied"),
                       ("reserved", "Reserved"), ("maintenance", "Blocked")):
        w.writerow([label, a["seats"][key]["count"], a["seats"][key]["pct"]])
    w.writerow(["Occupied - walk-in (camera)", a["occupied"]["walkin"]["count"], a["occupied"]["walkin"]["pct"]])
    w.writerow(["Occupied - reserved check-in", a["occupied"]["checkedIn"]["count"], a["occupied"]["checkedIn"]["pct"]])
    w.writerow([])
    w.writerow(["Zone", "Seats", "Occupied", "Occupied %", "Reserved", "Reserved %", "Available", "Available %"])
    for z in a["zones"]:
        w.writerow([z["name"], z["total"], z["occupied"]["count"], z["occupied"]["pct"], z["reserved"]["count"],
                    z["reserved"]["pct"], z["available"]["count"], z["available"]["pct"]])
    w.writerow([])
    r = a["reservations"]
    w.writerow(["Reservations", "Count", "% of all reservations"])
    w.writerow(["Today", r["today"], ""])
    w.writerow(["This week", r["week"], ""])
    w.writerow(["All time", r["allTime"], ""])
    for key, label in (("awaitingApproval", "Awaiting approval"), ("pending", "Accepted, waiting for check-in"),
                       ("inSession", "In session"), ("completed", "Completed"),
                       ("cancelled", "Cancelled before check-in"), ("expired", "Expired (no-show)"),
                       ("rejected", "Rejected")):
        w.writerow([label, r["status"][key]["count"], r["status"][key]["pct"]])
    w.writerow(["Active rate", "", r["activeRate"]])
    w.writerow(["Fulfillment rate (showed up)", "", r["fulfillmentRate"]])
    w.writerow(["No-show / cancellation rate", "", r["noShowRate"]])
    w.writerow([])
    w.writerow(["Role", "Reservations", "% share"])
    for role in a["roles"]:
        w.writerow([role["label"], role["count"], role["pct"]])
    w.writerow([])
    w.writerow(["Hour", "Reservations made", "Check-ins / walk-ins"])
    for hour in range(24):
        w.writerow([f"{hour:02d}:00", a["hours"]["reservations"][hour], a["hours"]["occupancy"][hour]])
    w.writerow([])
    w.writerow(["Top seats", "Zone", "Sessions", "% of all sessions", "Minutes in use"])
    for s in a["topSeats"]:
        w.writerow([s["seat"], s["zone"], s["count"], s["pct"], s["minutes"]])
    w.writerow([])
    _write_forecast_rows(w, services.get_predictive_analytics())
    return response


def _write_forecast_rows(w, f):
    """The predictive section of the CSV exports (shared by analytics.csv and forecast.csv)."""
    meta, cap, ns = f["meta"], f["capacity"], f["noShow"]
    w.writerow(["Predictive analytics (forecast)", f["generatedAt"][:16].replace("T", " ")])
    w.writerow(["Method", meta["mode"], meta["note"]])
    w.writerow(["Days of history used", meta["historyDays"], f"confidence: {meta['confidence']}"])
    w.writerow([])
    w.writerow(["Predicted high-demand windows (today)", "Avg predicted occupancy %", "Peak %"])
    for win in f["windows"]:
        w.writerow([win["message"], win["avgPct"], win["peakPct"]])
    w.writerow([])
    w.writerow(["Hour (today)", "Predicted occupancy %", "Already booked %", "Risk"])
    for h in f["hourly"]:
        w.writerow([h["label"], h["pct"], h["bookedPct"], h["riskLabel"]])
    w.writerow([])
    w.writerow(["Next 7 days", "Date", "Predicted reservations", "Students", "Professors", "Booked so far",
                "Avg utilization %", "Peak utilization %", "Peak hour", "Expected no-shows", "Risk"])
    for d in f["days"]:
        w.writerow([d["weekday"], d["date"], d["reservations"], d["student"], d["professor"], d["bookedSoFar"],
                    d["avgUtilization"], d["peakUtilization"], d["peakHourLabel"], d["expectedNoShows"], d["riskLabel"]])
    w.writerow([])
    w.writerow(["Bottleneck seat", "Zone", "Days used %", "Peak hour", "Peak probability %", "Projected taken from"])
    for s in f["bottlenecks"]["seats"]:
        w.writerow([s["seat"], s["zone"], s["frequency"], s["peakHourLabel"], s["peakProbability"],
                    s["projectedFullLabel"] or "-"])
    w.writerow([])
    w.writerow(["Bottleneck zone", "Seats", "Peak occupancy %", "Peak hour", "Projected high demand from", "Risk"])
    for z in f["bottlenecks"]["zones"]:
        w.writerow([z["name"], z["seats"], z["peakPct"], z["peakHourLabel"], z["projectedFullLabel"] or "-", z["riskLabel"]])
    w.writerow([])
    w.writerow(["No-show forecast", "Value"])
    w.writerow(["Expected no-show rate %", ns["rate"]])
    w.writerow(["  students %", ns["byRole"]["student"]])
    w.writerow(["  professors %", ns["byRole"]["professor"]])
    w.writerow(["Expected no-shows per day", ns["perDay"]])
    w.writerow(["Expected no-shows, next 7 days", ns["nextSevenDays"]])
    w.writerow([])
    w.writerow(["Capacity recommendation", "Value"])
    w.writerow(["Predicted peak occupancy %", cap["peakPct"], cap["peakDay"]])
    w.writerow(["Peak window", cap["peakWindow"] or "-"])
    w.writerow(["Recommended walk-in buffer (seats)", cap["recommendedWalkInBuffer"]])
    w.writerow(["No-show over-booking headroom (seats)", cap["overbookHeadroom"]])
    for rec in f["recommendations"]:
        w.writerow([f"Recommendation - {rec['title']}", rec["text"]])


@_admin_only
def forecast_csv(request):
    f = services.get_predictive_analytics()
    response = HttpResponse(content_type="text/csv")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="libraryos-forecast-{stamp}.csv"'
    _write_forecast_rows(csv.writer(response), f)
    return response


def _report_range(request):
    """(start, end) dates from ?start=YYYY-MM-DD&end=YYYY-MM-DD; defaults to today. Raises BookingError."""
    try:
        start = parse_date(request.GET.get("start") or "") or timezone.localdate()
        end = parse_date(request.GET.get("end") or "") or start
    except ValueError:
        raise BookingError("Dates must look like YYYY-MM-DD.", code="invalid_input")
    return start, end


@never_cache
@require_GET
def api_analytics(request):
    profile = _profile(request.user)
    if profile is None:
        return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
    if profile.role != Profile.Role.ADMIN:
        return JsonResponse({"ok": False, "error": "Admins only.", "code": "forbidden"}, status=403)
    try:
        report = services.get_date_report(*_report_range(request))
    except BookingError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "code": exc.code}, status=exc.status)
    return JsonResponse({"ok": True, "report": report})


@_admin_only
def date_report_csv(request):
    try:
        r = services.get_date_report(*_report_range(request))
    except BookingError as exc:
        return HttpResponse(exc.message, status=exc.status, content_type="text/plain")
    response = HttpResponse(content_type="text/csv")
    label = r["start"] if r["start"] == r["end"] else f"{r['start']}_to_{r['end']}"
    response["Content-Disposition"] = f'attachment; filename="libraryos-report-{label}.csv"'
    w = csv.writer(response)
    w.writerow(["LibraryOS analytics report", r["start"], r["end"]])
    w.writerow([])
    q = r["requests"]
    w.writerow(["Reservation requests", "Count", "% of requests"])
    w.writerow(["Total requested", q["total"], ""])
    for key, name in (("awaitingApproval", "Still awaiting approval"), ("accepted", "Accepted"),
                      ("verified", "Checked in (code verified)"), ("noShow", "No-show / expired (15-minute grace)"),
                      ("forcedOrCancelled", "Force-released or cancelled"), ("forceReleased", "  of which force-released"),
                      ("cancelled", "  of which cancelled"), ("rejected", "Rejected")):
        w.writerow([name, q[key]["count"], q[key]["pct"]])
    w.writerow([])
    o = r["occupancy"]
    w.writerow(["Occupancy", "Value"])
    w.writerow(["Seats in the library", o["seatTotal"]])
    w.writerow(["Seats occupied at some point", o["seatsUsed"]])
    w.writerow(["Occupancy rate %", o["rate"]])
    w.writerow(["Sessions", o["sessions"]])
    w.writerow(["Walk-in sessions", o["walkin"]["sessions"], f"{o['walkin']['pct']}%"])
    w.writerow(["Reserved sessions", o["reserved"]["sessions"], f"{o['reserved']['pct']}%"])
    w.writerow(["Seats used by walk-ins", o["walkin"]["seats"], f"{o['walkin']['seatPct']}% of seats"])
    w.writerow(["Seats used by reservations", o["reserved"]["seats"], f"{o['reserved']['seatPct']}% of seats"])
    w.writerow([])
    w.writerow(["Role", "Requests", "% share"])
    for role in r["roles"]:
        w.writerow([role["label"], role["count"], role["pct"]])
    w.writerow([])
    w.writerow(["Most used seats", "Zone", "Sessions", "% of sessions", "Minutes in use"])
    for t in r["topSeats"]:
        w.writerow([t["seat"], t["zone"], t["count"], t["pct"], t["minutes"]])
    return response


# -------------------------------------------------------------------- pages
@ensure_csrf_cookie
def dashboard(request, page_role):
    """Each role has its own URL. Signed-in users are always sent to their own dashboard."""
    profile = _profile(request.user)
    if profile is None:
        return redirect("seats:login")
    if profile.role != page_role:
        return _home_for(profile)

    role, user = profile.role, request.user
    state = services.build_state(role, user)
    config = {
        "pollMs": POLL_INTERVAL_MS,
        "urls": {
            "state": reverse("seats:state"),
            "live": reverse("seats:live"),
            "batchReserve": reverse("seats:batch-reserve"),
            "reserve": reverse("seats:reserve", args=[SEAT_PLACEHOLDER]),
            "release": reverse("seats:release", args=[SEAT_PLACEHOLDER]),
            "maintenance": reverse("seats:maintenance", args=[SEAT_PLACEHOLDER]),
            "accept": reverse("seats:accept", args=[ID_PLACEHOLDER]),
            "reject": reverse("seats:reject", args=[ID_PLACEHOLDER]),
            "cancel": reverse("seats:cancel", args=[ID_PLACEHOLDER]),
            "verify": reverse("seats:verify"),
            "notificationsRead": reverse("seats:notifications-read"),
            "nameRequest": reverse("seats:name-request"),
            "analytics": reverse("seats:analytics"),
            "dateReportCsv": reverse("seats:date-report-csv"),
            "schedule": reverse("seats:schedule", args=[SEAT_PLACEHOLDER]),
            "releaseReservation": reverse("seats:release-reservation", args=[ID_PLACEHOLDER]),
            "forecast": reverse("seats:forecast"),
            "forecastCsv": reverse("seats:forecast-csv"),
        },
        "slots": {
            "minMinutes": int(services.MIN_SLOT.total_seconds() // 60),
            "studentMaxMinutes": int(services.STUDENT_MAX_SLOT.total_seconds() // 60),
            "professorMaxMinutes": int(services.PROFESSOR_MAX_SLOT.total_seconds() // 60),
            "adminMaxMinutes": int(services.ADMIN_MAX_SLOT.total_seconds() // 60),
            "maxAdvanceDays": services.MAX_ADVANCE_DAYS,
        },
        "serverClock": timezone.localtime().strftime("%H:%M"),
        "seatPlaceholder": SEAT_PLACEHOLDER,
        "idPlaceholder": ID_PLACEHOLDER,
        "serverTime": timezone.now().isoformat(),
        "serverDate": timezone.localdate().isoformat(),
    }
    context = {
        "role": role,
        "current_user": services.identity(user),
        "zones": state["zones"],
        "kpis": state["summary"],
        "state": state,
        "analytics": state.get("analytics"),
        "config": config,
        "pending_count": services.pending_account_request_count() if role == "admin" else 0,
    }
    return render(request, "seats/index.html", context)


# ---------------------------------------------------------------------- API
@never_cache
@require_GET
def api_state(request):
    profile = _profile(request.user)
    if profile is None:
        return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
    return JsonResponse({"ok": True, "state": services.build_state(profile.role, request.user)})


@never_cache
@require_GET
def api_live(request):
    """Lightweight poll target: current seat statuses + counts (camera changes show up here)."""
    profile = _profile(request.user)
    if profile is None:
        return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
    state = services.build_state(profile.role, request.user)
    return JsonResponse({"ok": True, "state": state, "serverTime": timezone.now().isoformat()})


@never_cache
@require_GET
def api_seat_schedule(request, seat_id):
    """A seat's reservation queue for one date (?date=YYYY-MM-DD, default today). Any signed-in role."""
    profile = _profile(request.user)
    if profile is None:
        return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
    try:
        day = parse_date(request.GET.get("date") or "") if request.GET.get("date") else None
    except ValueError:
        day = None
    if request.GET.get("date") and day is None:
        return JsonResponse({"ok": False, "error": "Dates must look like YYYY-MM-DD.", "code": "invalid_input"}, status=400)
    services.expire_overdue_reservations()
    try:
        schedule = services.get_seat_schedule(request.user, profile.role, seat_id, day)
    except BookingError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "code": exc.code}, status=exc.status)
    return JsonResponse({"ok": True, "schedule": schedule})


@never_cache
@require_GET
def api_forecast(request):
    """Predictive analytics for the Admin dashboard."""
    profile = _profile(request.user)
    if profile is None:
        return JsonResponse({"ok": False, "error": "Please sign in.", "code": "unauthenticated"}, status=401)
    if profile.role != Profile.Role.ADMIN:
        return JsonResponse({"ok": False, "error": "Admins only.", "code": "forbidden"}, status=403)
    return JsonResponse({"ok": True, "forecast": services.get_predictive_analytics()})


@api_post
def reserve_seat(request, role, user, seat_id):
    _require_role(role, "student")
    data = _json_body(request)
    return services.reserve_seat_for_student(
        user, seat_id, data.get("duration"), data.get("start_hour"),
        date=data.get("date"), start_time=data.get("start_time"), end_time=data.get("end_time"),
    )


@api_post
def batch_reserve(request, role, user):
    _require_role(role, "professor", "admin")
    data = _json_body(request)
    return services.batch_reserve_for_professor(
        user, data.get("seat_ids"), data.get("title"), data.get("duration"), data.get("start_hour"),
        date=data.get("date"), start_time=data.get("start_time"), end_time=data.get("end_time"),
    )


@api_post
def release_seat(request, role, user, seat_id):
    return services.release_seat(role, user, seat_id)


@api_post
def toggle_maintenance(request, role, user, seat_id):
    _require_role(role, "admin")
    return services.toggle_maintenance(user, seat_id)


@api_post
def accept_reservation(request, role, user, reservation_id):
    _require_role(role, "admin")
    return services.accept_reservation(user, reservation_id)


@api_post
def reject_reservation(request, role, user, reservation_id):
    _require_role(role, "admin")
    return services.reject_reservation(user, reservation_id)


@api_post
def release_reservation(request, role, user, reservation_id):
    _require_role(role, "admin")
    return services.admin_release_reservation(user, reservation_id)


@api_post
def cancel_reservation(request, role, user, reservation_id):
    return services.cancel_reservation(user, reservation_id, seat_only=bool(_json_body(request).get("seat_only")))


@api_post
def verify_code(request, role, user):
    _require_role(role, "admin")
    return services.verify_confirmation_code(user, _json_body(request).get("code"))


@api_post
def notifications_read(request, role, user):
    ids = _json_body(request).get("ids")
    services.mark_notifications_read(user, ids if isinstance(ids, list) else None)
    return "", "info"


@api_post
def profile_name_request(request, role, user):
    _require_role(role, "student", "professor")
    form = ProfileNameForm(_json_body(request))
    if not form.is_valid():
        raise BookingError("Please enter a valid first and last name (up to 80 characters each).", code="invalid_input")
    data = form.cleaned_data
    return services.request_name_change(user, data["first_name"], data["middle_name"], data["last_name"])
