"""Coruscant Guard VIP tracker.

Watches the VIP ranks in the GAR and CG Roblox groups and posts to a Discord
webhook when one of them joins or leaves Star Wars: Roleplay.

Run normally:     python bot.py
Check the setup:  python bot.py --check   (prints results, posts nothing)
"""
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROLES_URL = "https://groups.roblox.com/v1/groups/{group}/roles"
MEMBERS_URL = "https://groups.roblox.com/v1/groups/{group}/roles/{role}/users"
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
GAME_URL = "https://www.roblox.com/games/4238077359/Star-Wars-Roleplay"
PUBLIC_SERVERS_URL = "https://games.roblox.com/v1/games/{place_id}/servers/Public"

IN_GAME_PRESENCE = 2
PRESENCE_BATCH = 30
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_ATTEMPTS = 5
ROSTER_REFRESH_SECONDS = 30 * 60
COOKIE_CHECK_SECONDS = 60 * 60
PUBLIC_SERVERS_CACHE_SECONDS = 60  # Refresh public server list every minute
FAILURES_BEFORE_WARNING = 3
DOWNTIME_NOTICE_SECONDS = 120
MAX_BACKOFF_SECONDS = 300

log = logging.getLogger("vip-tracker")


@dataclass
class Vip:
    user_id: int
    username: str
    group: str
    rank: str
    tier: str
    studs: int
    colour: int
    note: str = ""


class RobloxClient:
    def __init__(self, cookie="", session=None):
        self.session = session or requests.Session()
        self.csrf = ""
        self.cookie = cookie
        if cookie:
            self.session.headers["Cookie"] = f".ROBLOSECURITY={cookie}"

    def _get(self, url, **kwargs):
        response = self.session.get(url, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()

    def roles(self, group_id):
        return self._get(ROLES_URL.format(group=group_id))["roles"]

    def members(self, group_id, role_id):
        cursor, members = "", []
        while True:
            page = self._get(
                MEMBERS_URL.format(group=group_id, role=role_id),
                params={"limit": 100, "sortOrder": "Asc", "cursor": cursor},
            )
            members.extend(page["data"])
            cursor = page.get("nextPageCursor")
            if not cursor:
                return members

    def presences(self, user_ids):
        results = []
        for start in range(0, len(user_ids), PRESENCE_BATCH):
            results.extend(self._presence_batch(user_ids[start:start + PRESENCE_BATCH]))
        return results

    def _presence_batch(self, user_ids):
        for _ in range(2):
            headers = {"x-csrf-token": self.csrf} if self.csrf else {}
            response = self.session.post(
                PRESENCE_URL, json={"userIds": user_ids}, headers=headers, timeout=20
            )
            token = response.headers.get("x-csrf-token")
            if response.status_code == 403 and token:
                self.csrf = token
                continue
            response.raise_for_status()
            return response.json()["userPresences"]
        raise RuntimeError("Roblox rejected the presence request after refreshing the token")

    def public_servers(self, place_id):
        """Returns a set of gameIds for all public servers of a place, paging through all results."""
        server_ids = set()
        cursor = ""
        while True:
            params = {"limit": 100, "sortOrder": "Asc"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get(PUBLIC_SERVERS_URL.format(place_id=place_id), params=params)
            except requests.RequestException:
                # If the request fails, return whatever we have so far.
                break
            for server in data.get("data", []):
                if server.get("id"):
                    server_ids.add(server["id"])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        return server_ids

    def cookie_account(self):
        """Returns the logged-in account name, or None if the cookie is not valid."""
        if not self.cookie:
            return None
        response = self.session.get(AUTH_URL, timeout=20)
        if response.status_code == 401:
            return None
        response.raise_for_status()
        return response.json().get("name")


class Discord:
    def __init__(self, webhook_url, ping_role_id="", session=None):
        self.url = webhook_url
        self.ping_role_id = ping_role_id
        self.session = session or requests.Session()
        # Alerts can be hours apart, and a reused idle connection may already be dead.
        self.session.headers["Connection"] = "close"

    def send(self, embeds, ping=False):
        for start in range(0, len(embeds), DISCORD_EMBEDS_PER_MESSAGE):
            batch = embeds[start:start + DISCORD_EMBEDS_PER_MESSAGE]
            payload = {"embeds": batch, "allowed_mentions": {"parse": []}}
            if ping and start == 0 and self.ping_role_id:
                pinged = dict(payload, content=f"<@&{self.ping_role_id}>",
                              allowed_mentions={"roles": [self.ping_role_id]})
                if self._post(pinged):
                    continue
                log.error("The ping failed, so the alert is being resent without it")
            if self._post(payload):
                continue
            # Last resort: an alert in plain text still beats a lost alert.
            text = "\n".join(
                f'{embed.get("title", "")}: {embed.get("description", "")}' for embed in batch
            )
            self._post({"content": text[:1900], "allowed_mentions": {"parse": []}})

    def _post(self, payload):
        for attempt in range(DISCORD_ATTEMPTS):
            try:
                response = self.session.post(self.url, json=payload, timeout=20)
            except requests.RequestException as error:
                # The exception text can contain the webhook URL, so log only its type.
                log.warning("Discord could not be reached (try %s of %s): %s",
                            attempt + 1, DISCORD_ATTEMPTS, type(error).__name__)
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                time.sleep(float(response.json().get("retry_after", 2)) + 0.25)
                continue
            if response.ok:
                return True
            log.error("Discord rejected a message: HTTP %s %s",
                      response.status_code, response.text[:300])
            return False
        log.error("Discord could not take this message after %s tries", DISCORD_ATTEMPTS)
        return False


def build_roster(roblox, config):
    """Returns (vips by user id, role names not found in Roblox)."""
    roster, missing = {}, []
    for group in config["groups"]:
        available = {role["name"].strip().casefold(): role for role in roblox.roles(group["id"])}
        for tier in group["tiers"]:
            for role_name in tier["roles"]:
                role = available.get(role_name.strip().casefold())
                if role is None:
                    missing.append(f'{group["name"]}: {role_name}')
                    continue
                for member in roblox.members(group["id"], role["id"]):
                    user_id = member.get("userId")
                    if user_id is None:
                        continue
                    vip = Vip(
                        user_id=user_id,
                        username=member.get("username") or str(user_id),
                        group=group["name"],
                        rank=tier.get("display_rank", role["name"]),
                        tier=tier["tier"],
                        studs=tier["studs"],
                        colour=int(tier["colour"].lstrip("#"), 16),
                        note=tier.get("note", ""),
                    )
                    # Someone in both groups keeps their largest killzone.
                    if user_id not in roster or vip.studs > roster[user_id].studs:
                        roster[user_id] = vip
    # Hand-added VIPs always win over whatever their group rank says.
    for extra in config.get("extra_vips", []):
        if not extra.get("user_id"):
            continue
        roster[extra["user_id"]] = Vip(
            user_id=extra["user_id"],
            username=extra["username"],
            group=extra.get("group", "GAR"),
            rank=extra["rank"],
            tier=extra["tier"],
            studs=extra["studs"],
            colour=int(extra["colour"].lstrip("#"), 16),
            note=extra.get("note", ""),
        )
    return roster, missing


def classify(presence, place_id, public_servers=None):
    if presence.get("userPresenceType") != IN_GAME_PRESENCE:
        return "out"
    root = presence.get("rootPlaceId")
    place = presence.get("placeId")
    if root is None and place is None:
        return "hidden"  # In some game, but Roblox will not say which.
    if place_id not in (root, place):
        return "out"
    # They are in the right game. If we have a public server list, check whether
    # their server is public. Private servers (Kamino, reserved slots, etc.) are
    # treated as "out" so guards are not alerted unnecessarily.
    game_id = presence.get("gameId")
    if public_servers is not None and game_id and game_id not in public_servers:
        log.debug("Skipping %s — server %s is not public (likely Kamino/private)", presence.get("userId"), game_id)
        return "private"
    return "in_game"


class Tracker:
    def __init__(self, roblox, discord, config, settings):
        self.roblox = roblox
        self.discord = discord
        self.config = config
        self.settings = settings
        self.place_id = config["game"]["place_id"]
        self.roster = {}
        self.state_path = Path(settings["state_file"])
        self.first_run = not self.state_path.exists()
        self.state = self._load_state()
        self._public_servers = None       # Cached set of public server gameIds
        self._public_servers_due = 0      # When to refresh the cache

    def _load_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        self.state["_last_alive"] = time.time()
        self.state.pop("_stopped_cleanly", None)
        self._write_state()

    def mark_stopped(self):
        """Records that the offline notice was already posted for this stop."""
        self.state["_stopped_cleanly"] = True
        self._write_state()

    def unnoticed_downtime(self):
        """Returns when the last run went quiet, if it died without saying so."""
        last_alive = self.state.get("_last_alive")
        if not last_alive or self.state.get("_stopped_cleanly"):
            return None
        return last_alive if time.time() - last_alive >= DOWNTIME_NOTICE_SECONDS else None

    def _write_state(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state))
        temporary.replace(self.state_path)

    def refresh_roster(self):
        roster, missing = build_roster(self.roblox, self.config)
        if roster:
            self.roster = roster
        return missing

    def _get_public_servers(self, now):
        """Returns the cached public server set, refreshing if stale."""
        if now >= self._public_servers_due:
            try:
                self._public_servers = self.roblox.public_servers(self.place_id)
                log.debug("Public server list refreshed: %d servers", len(self._public_servers))
            except Exception as error:
                log.warning("Could not refresh public server list: %s", type(error).__name__)
                # Keep using the old cache rather than blocking all alerts.
            self._public_servers_due = now + PUBLIC_SERVERS_CACHE_SECONDS
        return self._public_servers

    def poll(self, now=None):
        """Checks everyone once and returns the alerts to send as (embed, ping)."""
        now = now or time.time()
        presences = self.roblox.presences(list(self.roster))
        by_user = {item["userId"]: item for item in presences}
        public_servers = self._get_public_servers(now)
        joined, left, hidden_new = [], [], []

        for user_id, vip in self.roster.items():
            key = str(user_id)
            record = self.state.setdefault(
                key, {"status": "out", "missed": 0, "pinged_at": 0, "server": None}
            )
            presence = by_user.get(user_id)
            current = classify(presence, self.place_id, public_servers) if presence else "out"
            # Treat private server the same as "out" for status tracking purposes.
            if current == "private":
                current = "out"
            previous = record["status"]

            if current == "in_game":
                record["missed"] = 0
                record["server"] = presence.get("gameId") or record["server"]
                if previous != "in_game":
                    record["status"] = "in_game"
                    joined.append(vip)
            elif previous == "in_game" and current == "hidden":
                record["missed"] = 0  # Assume they stayed; the game is just not visible.
            elif previous == "in_game":
                # Roblox presence flickers, so a leave needs several misses in a row.
                record["missed"] += 1
                if record["missed"] >= self.settings["offline_polls"]:
                    record.update(status="out", missed=0, server=None)
                    left.append(vip)
            elif current == "hidden" and previous == "out":
                record["status"] = "hidden"
                hidden_new.append(vip)
            elif current == "out" and previous == "hidden":
                record["status"] = "out"

        alerts = []
        if not self.first_run:
            alerts += [self._join_alert(vip, now) for vip in joined]
            alerts += [(self._embed(vip, "VIP left the game", 0xD32F2F, now), False) for vip in left]
            if self.settings["post_hidden"]:
                alerts += [(self._hidden_embed(vip, now), False) for vip in hidden_new]
        self._save_state()
        return alerts

    def in_game(self):
        return [
            vip for user_id, vip in self.roster.items()
            if self.state.get(str(user_id), {}).get("status") == "in_game"
        ]

    def _embed(self, vip, title, colour, now):
        fields = [
            {"name": "Rank", "value": f"{vip.rank} ({vip.group})", "inline": True},
            {"name": "Killzone", "value": f"{vip.studs} studs", "inline": True},
            {
                "name": "Profile Link",
                "value": f"[{vip.username}](https://www.roblox.com/users/{vip.user_id}/profile)",
                "inline": True,
            },
        ]
        return {
            "title": title,
            "description": f"**{vip.username}** | {vip.tier}",
            "color": colour,
            "fields": fields,
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }

    def _join_alert(self, vip, now):
        record = self.state[str(vip.user_id)]
        embed = self._embed(vip, "VIP in game", vip.colour, now)
        embed["fields"].append({"name": "Game", "value": f"[Travel]({GAME_URL})", "inline": True})
        server = record.get("server")
        if server:
            same_rank = [
                other.username for other in self.in_game()
                if other.rank == vip.rank and other.user_id != vip.user_id
                and self.state[str(other.user_id)].get("server") == server
            ]
            if same_rank:
                embed["fields"].append({
                    "name": "Split guard",
                    "value": f"Same rank in this server: {', '.join(same_rank)}",
                    "inline": False,
                })
        if vip.note:
            embed["fields"].append({"name": "Note", "value": vip.note, "inline": False})
        embed["footer"] = {"text": "Confirm they are in morph before guarding."}

        cooldown = self.settings["ping_cooldown_seconds"]
        ping = now - record["pinged_at"] >= cooldown
        if ping:
            record["pinged_at"] = now
        return embed, ping

    def _hidden_embed(self, vip, now):
        embed = self._embed(vip, "VIP in an unknown game", 0xF9A825, now)
        embed["footer"] = {"text": "Their privacy settings hide which game this is."}
        return embed


def notice(title, description, colour=0x607D8B):
    return {"title": title, "description": description, "color": colour}


def startup_message(tracker, missing, account):
    lines = [f"Tracking **{len(tracker.roster)}** VIPs in {tracker.config['game']['name']}."]
    lines.append(f"Roblox account: **{account}**" if account else
                 "**No valid Roblox cookie.** The bot cannot confirm which game VIPs are in.")
    names = [vip.username for vip in tracker.in_game()]
    lines.append("In game now: " + (", ".join(names) if names else "nobody"))
    if missing:
        lines.append("Roles not found: " + ", ".join(missing))
    return notice("VIP tracker online", "\n".join(lines))


def downtime_notice(since):
    return notice(
        "VIP tracker was offline",
        f"It stopped responding around <t:{int(since)}:t> and is back now. "
        "VIP alerts from that time were missed.",
        0xD32F2F,
    )


def run_check(tracker, roblox):
    account = roblox.cookie_account()
    print(f"Cookie account: {account or 'NONE, game details will be hidden'}")
    missing = tracker.refresh_roster()
    print(f"VIPs found: {len(tracker.roster)}")
    for vip in sorted(tracker.roster.values(), key=lambda item: (-item.studs, item.rank, item.username)):
        print(f"  {vip.studs:>2} studs  {vip.group:<3}  {vip.rank:<22}  {vip.username}")
    if missing:
        print("Roles not found (fix the spelling in config.json):")
        for role in missing:
            print(f"  {role}")
    counts = {"in_game": 0, "hidden": 0, "out": 0}
    if tracker.roster:
        for presence in roblox.presences(list(tracker.roster)):
            counts[classify(presence, tracker.place_id)] += 1
    print(f"Presence now: {counts['in_game']} in this game, "
          f"{counts['hidden']} in an unknown game, {counts['out']} elsewhere or offline")


def run(tracker, roblox, discord, settings):
    """Runs the bot and says so in Discord whenever it stops."""
    try:
        watch(tracker, roblox, discord, settings)
    except KeyboardInterrupt:
        discord.send([notice("VIP tracker offline", "The bot was stopped. No alerts until it restarts.",
                             0xD32F2F)])
        tracker.mark_stopped()
    except Exception as error:
        discord.send([notice("VIP tracker offline",
                             f"The bot crashed ({type(error).__name__}). No alerts until it restarts.",
                             0xD32F2F)])
        tracker.mark_stopped()
        raise


def watch(tracker, roblox, discord, settings):
    down_since = tracker.unnoticed_downtime()
    missing = tracker.refresh_roster()
    if not tracker.roster:
        raise SystemExit("No VIPs found. Run python bot.py --check to see why.")
    alerts = tracker.poll()  # Empty on the very first start; catches joins after a restart.
    tracker.first_run = False
    account = roblox.cookie_account()
    if down_since:
        discord.send([downtime_notice(down_since)])
    discord.send([startup_message(tracker, missing, account)])
    for embed, ping in alerts:
        discord.send([embed], ping=ping)

    roster_due = time.time() + ROSTER_REFRESH_SECONDS
    cookie_due = time.time() + COOKIE_CHECK_SECONDS
    cookie_warned = not account
    failures = 0
    outage_warned = False
    last_tick = time.time()

    while True:
        delay = settings["poll_seconds"]
        # A jump in the clock means the computer slept with the bot frozen.
        if time.time() - last_tick - settings["poll_seconds"] >= DOWNTIME_NOTICE_SECONDS:
            discord.send([downtime_notice(last_tick)])
        try:
            if time.time() >= roster_due:
                tracker.refresh_roster()
                roster_due = time.time() + ROSTER_REFRESH_SECONDS
            alerts = tracker.poll()
            for embed, ping in alerts:
                discord.send([embed], ping=ping)
            failures = 0
            if outage_warned:
                discord.send([notice("VIP tracker recovered", "Roblox is responding again.", 0x2E7D32)])
                outage_warned = False
            if time.time() >= cookie_due:
                cookie_due = time.time() + COOKIE_CHECK_SECONDS
                valid = roblox.cookie_account()
                if not valid and not cookie_warned:
                    discord.send([notice(
                        "VIP tracker needs a new cookie",
                        "The Roblox login expired, so game details are hidden. Update ROBLOSECURITY.",
                        0xD32F2F,
                    )])
                cookie_warned = not valid
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
            failures += 1
            log.warning("Check failed (%s): %s", failures, type(error).__name__)
            delay = min(settings["poll_seconds"] * 2 ** min(failures, 6), MAX_BACKOFF_SECONDS)
            if failures >= FAILURES_BEFORE_WARNING and not outage_warned:
                discord.send([notice(
                    "VIP tracker is having trouble",
                    "Roblox checks keep failing, so alerts may be missing.",
                    0xD32F2F,
                )])
                outage_warned = True
        last_tick = time.time() + delay - settings["poll_seconds"]
        time.sleep(delay)


def stop_on_signal(*_):
    raise KeyboardInterrupt


def load_settings():
    return {
        "poll_seconds": int(os.getenv("POLL_SECONDS", "30")),
        "offline_polls": int(os.getenv("OFFLINE_POLLS", "4")),
        "ping_cooldown_seconds": int(os.getenv("PING_COOLDOWN_MINUTES", "15")) * 60,
        "post_hidden": os.getenv("POST_HIDDEN", "false").lower() == "true",
        "state_file": os.getenv("STATE_FILE", str(HERE / "state.json")),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(HERE / ".env")
    config = json.loads((HERE / "config.json").read_text())
    settings = load_settings()
    roblox = RobloxClient(os.getenv("ROBLOSECURITY", "").strip())

    if "--check" in sys.argv:
        settings["state_file"] = str(HERE / "state.check.json")
        run_check(Tracker(roblox, None, config, settings), roblox)
        return

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK_URL is missing. Copy .env.example to .env and fill it in.")
    # SIGTERM is a host stopping the bot; SIGBREAK is Windows closing its window.
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop_on_signal)

    role_id = os.getenv("VIP_PING_ROLE_ID", "").strip()
    role_problem = bool(role_id) and not role_id.isdigit()
    if role_problem:
        log.error("VIP_PING_ROLE_ID must contain only numbers, so pings are off")
        role_id = ""
    discord = Discord(webhook, role_id)
    if role_problem:
        discord.send([notice(
            "VIP pings are off",
            "VIP_PING_ROLE_ID in .env must contain only numbers. Alerts will post without a ping.",
            0xD32F2F,
        )])
    elif not role_id:
        log.warning("VIP_PING_ROLE_ID is empty, so alerts will post without a ping")
    tracker = Tracker(roblox, discord, config, settings)
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        from slash_commands import start_commands
        start_commands(tracker, token)
    run(tracker, roblox, discord, settings)


if __name__ == "__main__":
    main()
