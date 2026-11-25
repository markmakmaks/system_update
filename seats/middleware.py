from django.shortcuts import redirect
from django.urls import reverse

# Pages a visitor may open without signing in. Everything else sends them to the Login page.
PUBLIC_PREFIXES = ("/static/", "/admin/", "/api/")  # /api/ answers with a JSON 401 instead of a redirect


class LoginRequiredMiddleware:
    """Visitors who are not signed in only ever see the Login, Sign-up and Forgot-password pages."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.public_paths = None

    def _public_paths(self):
        if self.public_paths is None:
            self.public_paths = {
                reverse("seats:login"),
                reverse("seats:signup"),
                reverse("seats:forgot-password"),
                reverse("seats:logout"),
            }
        return self.public_paths

    def __call__(self, request):
        path = request.path
        if (
            not request.user.is_authenticated
            and path not in self._public_paths()
            and not path.startswith(PUBLIC_PREFIXES)
        ):
            return redirect("seats:login")
        return self.get_response(request)


class NoCacheMiddleware:
    """Stop browsers storing pages, so Back / old tabs after sign-out can't show private content."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
        return response
