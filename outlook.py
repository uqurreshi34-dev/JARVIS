"""
Microsoft Outlook Calendar integration for JARVIS.

Uses Microsoft Graph with delegated authentication.
The user signs in once; MSAL caches the authentication state locally.

No Outlook desktop COM automation is required.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import msal


CLIENT_ID = "2ea0fef2-a249-4bd1-be44-49c635ad199a"

# Personal Microsoft accounts only.
AUTHORITY = "https://login.microsoftonline.com/consumers"

SCOPES = [
    "Calendars.ReadWrite",
]

GRAPH_URL = "https://graph.microsoft.com/v1.0/me/calendar/events"

_TOKEN_CACHE_FILE = "outlook_token_cache.json"

_lock = threading.Lock()


def _cache_path():
    """Return the path used to persist the MSAL token cache."""

    try:
        from actions import files

        root = files.root()
    except Exception:
        root = None

    if not root:
        return None

    return os.path.join(root, _TOKEN_CACHE_FILE)


def _load_cache():
    """Load the MSAL token cache from disk."""

    cache = msal.SerializableTokenCache()
    path = _cache_path()

    if not path or not os.path.exists(path):
        return cache

    try:
        with open(path, "r", encoding="utf-8") as handle:
            cache.deserialize(handle.read())
    except (OSError, ValueError) as error:
        print(f"[JARVIS] could not load Outlook token cache: {error}")

    return cache


def _save_cache(cache):
    """Persist the MSAL token cache if it changed."""

    if not cache.has_state_changed:
        return

    path = _cache_path()

    if not path:
        return

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(cache.serialize())
    except OSError as error:
        print(f"[JARVIS] could not save Outlook token cache: {error}")


def _application(cache):
    """Create the MSAL public-client application."""

    return msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )


def _access_token():
    """
    Get a Microsoft Graph access token.

    Normally this is silent because MSAL already has the user's
    authentication state cached.

    If no usable cached token exists, Microsoft opens the normal
    sign-in/consent experience in the browser.
    """

    with _lock:
        cache = _load_cache()
        app = _application(cache)

        accounts = app.get_accounts()

        result = None

        if accounts:
            result = app.acquire_token_silent(
                SCOPES,
                account=accounts[0],
            )

        if not result:
            print("[JARVIS] Outlook authentication required.")

            result = app.acquire_token_interactive(
                scopes=SCOPES,
            )

        _save_cache(cache)

    if not result or "access_token" not in result:
        error = (result or {}).get("error", "unknown_error")
        description = (result or {}).get(
            "error_description",
            "No access token was returned.",
        )

        raise RuntimeError(
            f"Microsoft authentication failed: {error}: {description}"
        )

    return result["access_token"]


def create_event(title, moment):
    """
    Create an event in the user's default Outlook calendar.

    Returns True when Microsoft Graph confirms the event was created.
    """

    try:
        token = _access_token()
    except Exception as error:
        print(f"[JARVIS] Outlook authentication failed: {error}")
        return False

    if hasattr(moment, "hour"):
        # Timed event.
        from datetime import timedelta

        start = moment
        end = moment + timedelta(hours=1)

        event = {
            "subject": title,
            "start": {
                "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Europe/London",
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Europe/London",
            },
            "isReminderOn": True,
            "reminderMinutesBeforeStart": 15,
        }

    else:
        # All-day event.
        from datetime import timedelta

        event = {
            "subject": title,
            "start": {
                "dateTime": moment.strftime("%Y-%m-%dT00:00:00"),
                "timeZone": "Europe/London",
            },
            "end": {
                "dateTime": (
                    moment + timedelta(days=1)
                ).strftime("%Y-%m-%dT00:00:00"),
                "timeZone": "Europe/London",
            },
            "isAllDay": True,
            "isReminderOn": False,
        }

    body = json.dumps(event).encode("utf-8")

    request = urllib.request.Request(
        GRAPH_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status == 201:
                print(f"[JARVIS] added to Outlook: {title}")
                return True

            print(
                f"[JARVIS] Outlook returned unexpected status "
                f"{response.status}"
            )
            return False

    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(error)

        print(
            f"[JARVIS] Outlook Graph error "
            f"{error.code}: {detail}"
        )
        return False

    except urllib.error.URLError as error:
        print(f"[JARVIS] could not reach Outlook: {error}")
        return False

    except Exception as error:
        print(f"[JARVIS] Outlook calendar error: {error}")
        return False


if __name__ == "__main__":
    from datetime import datetime

    ok = create_event(
        "JARVIS Test Appointment",
        datetime(2026, 8, 25, 10, 0),
    )

    print(f"Result: {ok}")
