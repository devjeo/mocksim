"""
main.py — interactive, menu-driven tool for creating a fake Smart Cane
device on your REAL Supabase project, attaching it to a real account, and
triggering fake pings/events so you can watch them show up live in the app.

Everything is picked by number from a menu - no command-line flags needed.
Just run:

    python main.py

Every device you create is remembered in mock_devices.json next to this
script. Re-run the script any time (even after a restart) and pick
"Use an existing mock device" - it will re-check live against Supabase
that the device and its link to that account still actually exist before
letting you do anything to it.

SECURITY NOTE: SUPABASE_SERVICE_ROLE_KEY below bypasses all Row Level
Security on your project - it's effectively a root password for your
database. It's hardcoded here because you said this script never leaves
your machine, which is fine for local testing, but:
  - never commit this file to git / share it / paste it anywhere public
  - delete mock devices when you're done testing (menu option is there)

PUSH NOTIFICATIONS (FCM): this script can also send real push
notifications to a device's guardian when you insert an event/log, using
Firebase's HTTP v1 API. That needs a *Firebase service account key*
(NOT hardcoded here - it's more sensitive than the Supabase key above,
since it can act as your whole Firebase project, not just one database).

To enable it:
  1. Firebase Console -> jeo-smartcane project -> Project settings ->
     Service accounts -> Generate new private key.
  2. Save the downloaded .json file next to this script, named exactly
     "firebase-service-account.json".
  3. pip install cryptography (only extra dependency this needs).
  4. Never commit that file either - add it to .gitignore.

If that file isn't present, push-sending is silently skipped (a message
is printed) - everything else in the script still works normally.
"""
import base64
import json
import os
import random
import string
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------
# CONFIG - fill in your service_role key below.
# --------------------------------------------------------------------------
SUPABASE_URL = "https://ldnspxkgplermswenlcw.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxkbnNweGtncGxlcm1zd2VubGN3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MzUxODU5NSwiZXhwIjoyMDg5MDk0NTk1fQ.DStTSiGxdW5VWeRK5KpRf_uu0ND3jJDfuqCtBj8gbZw"

REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_devices.json")

# Service account key for sending real FCM pushes - see the PUSH
# NOTIFICATIONS note above. Kept in its own file, never inline here.
FIREBASE_SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "firebase-service-account.json"
)
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

EVENT_TYPES = [
    "Geofence Breach",
    "Emergency",
    "Battery Low",
    "Safe / Resolved",
    "Vision Alert (Crosswalk)",
    "Custom...",
]

# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

def headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def rest(method, path, **kw):
    kw.setdefault("headers", headers())
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", timeout=15, **kw)
    if not r.ok:
        print(f"  !! {method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    return r


def new_device_id():
    return "MOCK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def get_device(device_id):
    rows = rest("GET", f"devices?id=eq.{device_id}&select=*").json()
    return rows[0] if rows else None


def fetch_real_users():
    r = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("users", data if isinstance(data, list) else [])


# --------------------------------------------------------------------------
# local registry (mock_devices.json)
# --------------------------------------------------------------------------

def load_registry():
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE) as f:
        return json.load(f)


def save_registry(entries):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(entries, f, indent=2)


def registry_add(device_id, user_id, email, name):
    entries = load_registry()
    entries.append({
        "device_id": device_id, "user_id": user_id, "email": email,
        "name": name, "created_at": now_iso(),
    })
    save_registry(entries)


def registry_remove(device_id):
    entries = [e for e in load_registry() if e["device_id"] != device_id]
    save_registry(entries)


# --------------------------------------------------------------------------
# small input helpers
# --------------------------------------------------------------------------

def prompt_index(prompt, count):
    """Ask for a 1-based index, return it as 0-based int, or None if blank/cancelled."""
    while True:
        raw = input(prompt).strip()
        if raw == "" or raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw) - 1
        print(f"  Enter a number between 1 and {count} (or 0 to cancel).")


def prompt_float(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    return float(raw) if raw else default


def prompt_str(prompt, default=""):
    raw = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return raw if raw else default


def confirm(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def pause():
    input("\n(press Enter to continue) ")


# --------------------------------------------------------------------------
# verification - re-checked live every time a stored device is selected
# --------------------------------------------------------------------------

def verify_link(entry):
    """Re-check against Supabase that this device still exists AND is
    still shared with the recorded user. Returns (ok, device_row_or_None, reason)."""
    device = get_device(entry["device_id"])
    if device is None:
        return False, None, "device no longer exists in the devices table"

    shares = rest("GET", f"device_shares?device_id=eq.{entry['device_id']}"
                          f"&user_id=eq.{entry['user_id']}").json()
    if not shares:
        return False, device, f"device exists, but is no longer linked to {entry['email']}"

    return True, device, "ok"


# --------------------------------------------------------------------------
# push notifications (FCM v1) - optional; skipped cleanly if not configured
# --------------------------------------------------------------------------

_fcm_token_cache = {"access_token": None, "expires_at": 0}
_service_account_cache = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_service_account():
    global _service_account_cache
    if _service_account_cache is None:
        if not os.path.exists(FIREBASE_SERVICE_ACCOUNT_FILE):
            raise FileNotFoundError(
                f"no {os.path.basename(FIREBASE_SERVICE_ACCOUNT_FILE)} next to main.py "
                "(see PUSH NOTIFICATIONS note at the top of this file)"
            )
        with open(FIREBASE_SERVICE_ACCOUNT_FILE) as f:
            _service_account_cache = json.load(f)
    return _service_account_cache


def _get_fcm_access_token():
    """Exchange the service account's private key for a short-lived OAuth2
    access token (FCM v1 needs this - the old server-key API is dead).
    Cached in-memory until ~1 min before it expires."""
    now = time.time()
    if _fcm_token_cache["access_token"] and now < _fcm_token_cache["expires_at"] - 60:
        return _fcm_token_cache["access_token"]

    sa = _load_service_account()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "iss": sa["client_email"],
        "scope": FCM_SCOPE,
        "aud": sa["token_uri"],
        "iat": int(now),
        "exp": int(now) + 3600,
    }).encode())
    signing_input = f"{header}.{payload}".encode()

    private_key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_assertion = f"{header}.{payload}.{_b64url(signature)}"

    r = requests.post(sa["token_uri"], data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_assertion,
    }, timeout=15)
    r.raise_for_status()
    token_data = r.json()
    _fcm_token_cache["access_token"] = token_data["access_token"]
    _fcm_token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _fcm_token_cache["access_token"]


def get_guardian_push_token(guardian_id):
    if not guardian_id:
        return None
    rows = rest("GET", f"profiles?id=eq.{guardian_id}&select=expo_push_token").json()
    if not rows:
        return None
    return rows[0].get("expo_push_token")


def send_push_for_device(device_id, title, body, data=None):
    """Look up the device's guardian and send them a real FCM push.
    Never raises out to the caller - by the time this runs, the actual
    log/device write already succeeded, so a broken push shouldn't look
    like the whole action failed. Failures just print a warning."""
    try:
        device = get_device(device_id)
        if device is None:
            print("  (push skipped: device not found)")
            return
        token = get_guardian_push_token(device.get("guardian_id"))
        if not token:
            print("  (push skipped: guardian has no push token saved yet - "
                  "have they opened the app since it was installed?)")
            return

        access_token = _get_fcm_access_token()
        sa = _load_service_account()
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in (data or {}).items()},
            }
        }
        r = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=message, timeout=15,
        )
        if r.status_code == 404 or "UNREGISTERED" in r.text:
            print("  !! Push failed: token is stale/unregistered - guardian "
                  "probably reinstalled the app or it's been too long. They "
                  "need to relaunch the app so it re-registers.")
            return
        r.raise_for_status()
        print("  Push sent to guardian.")
    except FileNotFoundError as e:
        print(f"  (push skipped: {e})")
    except requests.HTTPError as e:
        print(f"  !! Push failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        print(f"  !! Push failed: {e}")


# --------------------------------------------------------------------------
# menu: list real accounts
# --------------------------------------------------------------------------

def menu_list_users():
    print("\nFetching real accounts from Supabase...\n")
    users = fetch_real_users()
    if not users:
        print("No users found.")
        return
    print(f"{'#':>3}  {'email':30} user_id")
    for i, u in enumerate(users, 1):
        print(f"{i:>3}  {u.get('email',''):30} {u['id']}")


# --------------------------------------------------------------------------
# menu: create a new mock device
# --------------------------------------------------------------------------

def menu_create_device():
    print("\nFetching real accounts from Supabase...\n")
    users = fetch_real_users()
    if not users:
        print("No users found - nothing to attach a device to.")
        return

    print(f"{'#':>3}  email")
    for i, u in enumerate(users, 1):
        print(f"{i:>3}  {u.get('email','')}")
    idx = prompt_index("\nPick account by number (0 to cancel): ", len(users))
    if idx is None:
        return
    user = users[idx]

    name = prompt_str("Device name", "Test Cane")
    lat = prompt_float("Starting latitude", 13.1391)
    lng = prompt_float("Starting longitude", 123.7438)
    device_id = new_device_id()

    print(f"\nAbout to create:")
    print(f"  device_id = {device_id}")
    print(f"  name      = {name}")
    print(f"  account   = {user.get('email')} ({user['id']})")
    print(f"  location  = ({lat}, {lng})")
    if not confirm("Create this device and link it to that account?"):
        print("Cancelled.")
        return

    rest("POST", "devices", headers={**headers(), "Prefer": "resolution=merge-duplicates"}, json={
        "id": device_id, "name": name, "guardian_id": user["id"],
        "latitude": lat, "longitude": lng, "battery_level": 100,
        "activity_status": "idle", "is_online": True,
        "last_updated": now_iso(), "light_mode": False,
    })
    rest("POST", "device_shares", json={
        "device_id": device_id, "user_id": user["id"], "role": "admin",
    })
    registry_add(device_id, user["id"], user.get("email", ""), name)

    print(f"\nDone. '{device_id}' is now linked to {user.get('email')} "
          f"and saved to {os.path.basename(REGISTRY_FILE)}.")
    print("It should show up on that account's dashboard in the app now.")


# --------------------------------------------------------------------------
# menu: use an existing mock device
# --------------------------------------------------------------------------

def menu_use_existing_device():
    entries = load_registry()
    if not entries:
        print("\nNo mock devices in the registry yet. Create one first.")
        return

    print(f"\n{'#':>3}  device_id       name                 account")
    for i, e in enumerate(entries, 1):
        print(f"{i:>3}  {e['device_id']:15} {e['name']:20} {e['email']}")
    idx = prompt_index("\nPick a device by number (0 to cancel): ", len(entries))
    if idx is None:
        return
    entry = entries[idx]

    print(f"\nVerifying '{entry['device_id']}' is still linked to {entry['email']}...")
    ok, device, reason = verify_link(entry)
    if not ok:
        print(f"  !! Verification failed: {reason}")
        if confirm("Remove this stale entry from the local registry?"):
            registry_remove(entry["device_id"])
            print("Removed.")
        return
    print("  OK - verified live against Supabase.")

    device_action_menu(entry, device)


def device_action_menu(entry, device):
    while True:
        print(f"\n--- {entry['device_id']} ({entry['name']}) — {entry['email']} ---")
        print(f"  lat={device.get('latitude')} lng={device.get('longitude')} "
              f"battery={device.get('battery_level')}% last_updated={device.get('last_updated')}")
        print("  1) Ping (move it / update battery)")
        print("  2) Insert a fake event / log")
        print("  3) Set battery level")
        print("  4) Mark offline")
        print("  5) Refresh / show current state")
        print("  6) Delete this device")
        print("  7) Send a test push (no DB write)")
        print("  0) Back to main menu")
        choice = input("Choose: ").strip()

        if choice == "1":
            action_ping(entry, device)
        elif choice == "2":
            action_event(entry, device)
        elif choice == "3":
            action_battery(entry, device)
        elif choice == "4":
            action_offline(entry, device)
        elif choice == "5":
            device = get_device(entry["device_id"])
            if device is None:
                print("Device no longer exists.")
                return
        elif choice == "6":
            if action_delete(entry):
                return
        elif choice == "7":
            action_test_push(entry)
        elif choice == "0":
            return
        else:
            print("  Not a valid option.")

        if choice in ("1", "3", "4"):
            device = get_device(entry["device_id"]) or device


def action_ping(entry, device):
    lat = prompt_float("New latitude", device.get("latitude", 13.1391))
    lng = prompt_float("New longitude", device.get("longitude", 123.7438))
    battery_raw = input(f"New battery % [unchanged={device.get('battery_level')}]: ").strip()
    battery = int(battery_raw) if battery_raw else None

    print(f"\nCurrent: lat={device.get('latitude')} lng={device.get('longitude')} "
          f"battery={device.get('battery_level')}%")
    print(f"New:     lat={lat} lng={lng} battery={battery if battery is not None else '(unchanged)'}")
    if not confirm("Apply?"):
        print("Cancelled.")
        return

    body = {"latitude": lat, "longitude": lng, "last_updated": now_iso(), "is_online": True}
    if battery is not None:
        body["battery_level"] = battery
    rest("PATCH", f"devices?id=eq.{entry['device_id']}", json=body)
    rest("POST", "location_history", json={
        "device_id": entry["device_id"], "latitude": lat, "longitude": lng,
        "session_id": "mock-live-session",
    })
    print("Pinged.")


def action_event(entry, device):
    print("\nEvent type:")
    for i, t in enumerate(EVENT_TYPES, 1):
        print(f"  {i}) {t}")
    idx = prompt_index("Pick a type (0 to cancel): ", len(EVENT_TYPES))
    if idx is None:
        return
    event_type = EVENT_TYPES[idx]
    if event_type == "Custom...":
        event_type = prompt_str("Custom event type")

    message = prompt_str("Message", "")
    lat_raw = input(f"Latitude for this event [blank = none]: ").strip()
    lng_raw = input(f"Longitude for this event [blank = none]: ").strip()
    lat = float(lat_raw) if lat_raw else None
    lng = float(lng_raw) if lng_raw else None

    print(f"\nAbout to insert log: type={event_type!r} message={message!r}")
    if not confirm("Insert?"):
        print("Cancelled.")
        return

    rest("POST", "logs", json={
        "device_id": entry["device_id"], "event_type": event_type,
        "title": event_type, "message": message,
    })
    print("Inserted.")

    if lat is not None and lng is not None:
        rest("POST", "location_history", json={
            "device_id": entry["device_id"], "latitude": lat, "longitude": lng,
        })
        print("Also recorded a location_history point for this event.")

    send_push_for_device(
        entry["device_id"], title=event_type,
        body=message or f"{event_type} on {entry['name']}",
        data={"type": "log", "device_id": entry["device_id"], "event_type": event_type},
    )


def action_battery(entry, device):
    level = int(prompt_float("New battery level (0-100)", device.get("battery_level", 100)))
    if not confirm(f"Set battery to {level}%?"):
        print("Cancelled.")
        return
    rest("PATCH", f"devices?id=eq.{entry['device_id']}", json={"battery_level": level})
    print(f"Battery set to {level}%.")
    if level <= 20:
        rest("POST", "logs", json={
            "device_id": entry["device_id"], "event_type": "Battery Low",
            "title": "Battery low", "message": f"Battery dropped to {level}%.",
        })
        print("Also inserted a 'Battery Low' log.")
        send_push_for_device(
            entry["device_id"], title="Battery low",
            body=f"{entry['name']}'s battery dropped to {level}%.",
            data={"type": "log", "device_id": entry["device_id"], "event_type": "Battery Low"},
        )


def action_offline(entry, device):
    if not confirm("Back-date this device so the app shows it as Offline?"):
        print("Cancelled.")
        return
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    rest("PATCH", f"devices?id=eq.{entry['device_id']}", json={"is_online": False, "last_updated": stale})
    print("Will now show as Offline in the app.")


def action_test_push(entry):
    """Send a push straight away, bypassing logs/devices entirely - useful
    for checking the FCM wiring itself works before trusting it inside a
    real event flow."""
    title = prompt_str("Push title", "Test notification")
    body = prompt_str("Push body", f"Hello from the mock tool ({entry['device_id']})")
    send_push_for_device(entry["device_id"], title=title, body=body, data={"type": "test"})


def action_delete(entry):
    shares = rest("GET", f"device_shares?device_id=eq.{entry['device_id']}").json()
    log_rows = rest("GET", f"logs?device_id=eq.{entry['device_id']}&select=id").json()
    hist_rows = rest("GET", f"location_history?device_id=eq.{entry['device_id']}&select=id").json()

    print("\nThis will PERMANENTLY delete:")
    print(f"  - device row {entry['device_id']}")
    print(f"  - {len(shares)} device_shares row(s)")
    print(f"  - {len(log_rows)} logs row(s)")
    print(f"  - {len(hist_rows)} location_history row(s)")
    typed = input(f"Type the device id ('{entry['device_id']}') to confirm deletion: ").strip()
    if typed != entry["device_id"]:
        print("Did not match - cancelled.")
        return False

    rest("DELETE", f"device_shares?device_id=eq.{entry['device_id']}")
    rest("DELETE", f"logs?device_id=eq.{entry['device_id']}")
    rest("DELETE", f"location_history?device_id=eq.{entry['device_id']}")
    rest("DELETE", f"devices?id=eq.{entry['device_id']}")
    registry_remove(entry["device_id"])
    print(f"Deleted {entry['device_id']}.")
    return True


# --------------------------------------------------------------------------
# main menu
# --------------------------------------------------------------------------

def main():
    if SUPABASE_SERVICE_ROLE_KEY == "PASTE_YOUR_SERVICE_ROLE_KEY_HERE":
        sys.exit("Edit main.py and paste your real service_role key into "
                  "SUPABASE_SERVICE_ROLE_KEY at the top of the file first.")

    while True:
        print("\n===== Smart Cane Mock Device Tool =====")
        print("  1) List real accounts")
        print("  2) Create a new mock device")
        print("  3) Use an existing mock device")
        print("  0) Exit")
        choice = input("Choose: ").strip()

        try:
            if choice == "1":
                menu_list_users()
                pause()
            elif choice == "2":
                menu_create_device()
                pause()
            elif choice == "3":
                menu_use_existing_device()
            elif choice == "0":
                print("Bye.")
                return
            else:
                print("Not a valid option.")
        except requests.HTTPError:
            print("(request failed - see error above)")
            pause()
        except KeyboardInterrupt:
            print("\nCancelled.")


if __name__ == "__main__":
    main()