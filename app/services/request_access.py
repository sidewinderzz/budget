"""Login page's "request access" notification, sent server-side via Resend's HTTP
API (https://api.resend.com/emails), same client/dependency digest.py already uses.

Replaces an earlier version of this form that POSTed straight from the browser to
EmailJS's REST API. That relied entirely on EmailJS's dashboard-configured domain
restriction to stop abuse, which turned out not to actually be enforced (confirmed
2026-09-06 by calling the endpoint with a spoofed Origin header and getting a 200) --
anyone who read the three IDs out of login.html's source could send arbitrary email
through that account from anywhere, with no rate limit at all. Routing through the
app's own backend closes this for real: no client-visible keys, and the same
IP-based rate limiter every other unauthenticated route already uses.
"""
import httpx

from app import config
from app.services import email_theme

_TIMEOUT = httpx.Timeout(10.0)


class RequestAccessError(Exception):
    """Any failure sending via Resend -- bad/missing key, network error, non-2xx."""


def send_request_access_notification(
    name: str,
    email: str,
    message: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    if not config.RESEND_API_KEY:
        raise RequestAccessError("BUDGET_RESEND_API_KEY is not set.")
    if not config.REQUEST_ACCESS_TO_EMAIL:
        raise RequestAccessError("BUDGET_REQUEST_ACCESS_TO_EMAIL is not set.")

    body_lines = [
        f"Name: {name or '(not given)'}",
        f"Email: {email or '(not given)'}",
        "",
        message or "(no message)",
    ]
    html = email_theme.render(
        "Someone requested access",
        [
            ("Who", [f"Name: {name or '(not given)'}", f"Email: {email or '(not given)'}"]),
            ("Message", (message or "(no message)").splitlines() or ["(no message)"]),
        ],
        footer="Sent from the login page's request-access form. Add them from Settings → Users.",
    )
    payload = {
        "from": config.DIGEST_FROM_EMAIL,
        "to": [config.REQUEST_ACCESS_TO_EMAIL],
        "subject": "Budget: someone requested access",
        "text": "\n".join(body_lines),
        "html": html,
    }

    with httpx.Client(transport=transport, timeout=_TIMEOUT) as client:
        try:
            response = client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            )
        except httpx.HTTPError as exc:
            raise RequestAccessError(f"Could not reach Resend: {exc}") from exc

    if response.status_code >= 300:
        raise RequestAccessError(f"Resend returned {response.status_code}: {response.text}")
