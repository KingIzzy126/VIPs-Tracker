"""Offline tests. Run with: python -m pytest test_bot.py"""
import json
from pathlib import Path

import bot

PLACE = 4238077359
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())


class FakeRoblox:
    def __init__(self):
        self.now = {}
        self.fail = False

    def roles(self, group_id):
        names = {
            5214183: ["Officer Cadet", "Second Lieutenant", "Colonel", "Grand Marshal"],
            5369125: ["Company Officer", "Company Commander", "Overseer", "Supreme Chancellor"],
        }[group_id]
        return [{"id": group_id + i, "name": n} for i, n in enumerate(names)]

    def members(self, group_id, role_id):
        people = {
            5214183: [{"userId": 1, "username": "Cadet"}],
            5214184: [{"userId": 2, "username": "SecondLt"}, {"userId": 3, "username": "SecondLt2"}],
            5214185: [{"userId": 4, "username": "Col"}, {"userId": 9, "username": "Both"}],
            5369125: [{"userId": 5, "username": "CompanyOfficer"}],
            5369126: [{"userId": 9, "username": "Both"}],
            5369127: [{"userId": 7, "username": "Over"}],
            5369128: [{"userId": 8, "username": "Chancellor"}],
        }
        return people.get(role_id, [])

    def presences(self, ids):
        if self.fail:
            raise RuntimeError("down")
        return [{"userId": i, **self.now.get(i, {"userPresenceType": 0})} for i in ids]


def in_game(server="srv-1"):
    return {"userPresenceType": 2, "rootPlaceId": PLACE, "placeId": PLACE, "gameId": server}


def other_game():
    return {"userPresenceType": 2, "rootPlaceId": 111, "placeId": 111, "gameId": "x"}


def hidden():
    return {"userPresenceType": 2, "rootPlaceId": None, "placeId": None, "gameId": None}


def make(tmp_path, post_hidden=False):
    settings = {
        "poll_seconds": 30, "offline_polls": 3, "ping_cooldown_seconds": 900,
        "post_hidden": post_hidden, "state_file": str(tmp_path / "state.json"),
    }
    roblox = FakeRoblox()
    tracker = bot.Tracker(roblox, None, CONFIG, settings)
    tracker.refresh_roster()
    tracker.poll(1000)
    tracker.first_run = False
    return roblox, tracker


def titles(alerts):
    return [embed["title"] for embed, _ in alerts]


def test_only_killzone_ranks_are_vips(tmp_path):
    _, tracker = make(tmp_path)
    assert 1 not in tracker.roster  # Officer Cadet
    assert 5 not in tracker.roster  # Company Officer
    assert tracker.roster[2].studs == 3
    assert tracker.roster[4].studs == 5
    assert tracker.roster[8].studs == 12


def test_person_in_both_groups_keeps_largest_killzone(tmp_path):
    _, tracker = make(tmp_path)
    assert tracker.roster[9].studs == 5
    assert tracker.roster[9].rank == "Colonel"


def test_missing_role_is_reported(tmp_path):
    roblox = FakeRoblox()
    _, missing = bot.build_roster(roblox, CONFIG)
    assert "GAR: Captain" in missing


def test_extra_vip_is_added_and_blank_id_skipped():
    config = dict(CONFIG, extra_vips=[
        {"user_id": 0, "username": "Blank", "rank": "X", "tier": "X", "studs": 1, "colour": "#000000"},
        {"user_id": 4, "username": "Chief", "rank": "Supreme Chancellor", "tier": "Chancellorship",
         "studs": 12, "colour": "#FFFF00"},
    ])
    roster, _ = bot.build_roster(FakeRoblox(), config)
    assert 0 not in roster
    assert roster[4].studs == 12 and roster[4].username == "Chief"


def test_owner_rank_shows_as_supreme_chancellor():
    class Roblox(FakeRoblox):
        def roles(self, group_id):
            return [{"id": 77, "name": "Owner"}] if group_id == 5214183 else []

        def members(self, group_id, role_id):
            return [{"userId": 50, "username": "Vikinglaw"}]

    roster, _ = bot.build_roster(Roblox(), CONFIG)
    assert roster[50].rank == "Supreme Chancellor"
    assert roster[50].studs == 12


def test_join_pings_once_and_links_profile(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    alerts = tracker.poll(2000)
    assert titles(alerts) == ["VIP in game"] and alerts[0][1] is True
    fields = {f["name"]: f["value"] for f in alerts[0][0]["fields"]}
    assert "Star-Wars-Roleplay" in fields["Game"]
    assert "roblox.com/users/2/profile" in fields["Profile Link"]
    assert fields["Rank"] == "Second Lieutenant (GAR)" and fields["Killzone"] == "3 studs"
    assert tracker.poll(2030) == []


def test_flicker_does_not_alert(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    roblox.now[2] = {"userPresenceType": 0}
    assert tracker.poll(2030) == [] and tracker.poll(2060) == []
    roblox.now[2] = in_game()
    assert tracker.poll(2090) == []


def test_leave_after_consecutive_misses(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    roblox.now[2] = {"userPresenceType": 0}
    tracker.poll(2030)
    tracker.poll(2060)
    alerts = tracker.poll(2090)
    assert titles(alerts) == ["VIP left the game"] and alerts[0][1] is False


def test_rejoin_inside_cooldown_alerts_without_ping(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    roblox.now[2] = {"userPresenceType": 0}
    for t in (2030, 2060, 2090):
        tracker.poll(t)
    roblox.now[2] = in_game()
    alerts = tracker.poll(2120)
    assert titles(alerts) == ["VIP in game"] and alerts[0][1] is False


def test_other_game_never_alerts(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = other_game()
    assert tracker.poll(2000) == []


def test_hidden_is_quiet_unless_enabled(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = hidden()
    assert tracker.poll(2000) == []

    (tmp_path / "second").mkdir()
    roblox, tracker = make(tmp_path / "second", post_hidden=True)
    roblox.now[2] = hidden()
    alerts = tracker.poll(2000)
    assert titles(alerts) == ["VIP in an unknown game"] and alerts[0][1] is False


def test_in_game_turning_hidden_does_not_count_as_leaving(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    roblox.now[2] = hidden()
    for t in (2030, 2060, 2090, 2120):
        assert tracker.poll(t) == []


def test_split_guard_line(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    roblox.now[3] = in_game()
    embed, _ = tracker.poll(2030)[0]
    assert any(f["name"] == "Split guard" and "SecondLt" in f["value"] for f in embed["fields"])


def test_overseer_note(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[7] = in_game()
    embed, _ = tracker.poll(2000)[0]
    assert any("morph" in f["value"] for f in embed["fields"])


def test_vip_already_online_at_first_start_stays_silent(tmp_path):
    settings = {
        "poll_seconds": 30, "offline_polls": 3, "ping_cooldown_seconds": 900,
        "post_hidden": False, "state_file": str(tmp_path / "state.json"),
    }
    roblox = FakeRoblox()
    roblox.now[2] = in_game()
    tracker = bot.Tracker(roblox, None, CONFIG, settings)
    tracker.refresh_roster()
    assert tracker.poll(1000) == []
    assert [v.username for v in tracker.in_game()] == ["SecondLt"]


def test_restart_does_not_repeat_alerts(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    tracker.poll(2000)
    restarted = bot.Tracker(roblox, None, CONFIG, tracker.settings)
    restarted.refresh_roster()
    assert restarted.first_run is False
    assert restarted.poll(2030) == []


def test_discord_ping_only_when_asked_and_batches():
    class Response:
        status_code, ok = 200, True

    class Session:
        sent = []
        headers = {}

        def post(self, url, json, timeout):
            self.sent.append(json)
            return Response()

    session = Session()
    discord = bot.Discord("https://example.test", "123", session)
    discord.send([{"title": str(i)} for i in range(12)], ping=True)
    assert len(session.sent) == 2
    assert session.sent[0]["content"] == "<@&123>"
    assert "content" not in session.sent[1]
    assert session.sent[1]["allowed_mentions"] == {"parse": []}


def test_presence_refreshes_csrf_token():
    class Response:
        def __init__(self, status, headers=None, body=None):
            self.status_code, self.headers, self._body = status, headers or {}, body

        def raise_for_status(self):
            assert self.status_code == 200

        def json(self):
            return self._body

    class Session:
        headers = {}
        calls = []

        def post(self, url, json, headers, timeout):
            self.calls.append(headers)
            if not headers:
                return Response(403, {"x-csrf-token": "abc"})
            return Response(200, body={"userPresences": [{"userId": 1}]})

    session = Session()
    client = bot.RobloxClient("cookie", session)
    assert client.presences([1]) == [{"userId": 1}]
    assert session.calls[1] == {"x-csrf-token": "abc"}


class ScriptedSession:
    """Rejects any payload matching `reject`, and records every attempt."""

    headers = {}

    def __init__(self, reject):
        self.reject, self.sent = reject, []

    def post(self, url, json, timeout):
        self.sent.append(json)
        ok = not self.reject(json)

        class Response:
            status_code = 200 if ok else 400
            text = "bad"

        Response.ok = ok
        return Response()


def test_alert_survives_rejected_ping():
    session = ScriptedSession(lambda payload: "content" in payload)
    bot.Discord("https://example.test", "123", session).send([{"title": "VIP in game"}], ping=True)
    assert len(session.sent) == 2
    assert session.sent[1]["embeds"] == [{"title": "VIP in game"}]
    assert "content" not in session.sent[1]


def test_alert_survives_rejected_embed():
    session = ScriptedSession(lambda payload: "embeds" in payload)
    embed = {"title": "VIP in game", "description": "Name | Tier"}
    bot.Discord("https://example.test", "123", session).send([embed], ping=True)
    assert session.sent[-1]["content"] == "VIP in game: Name | Tier"


def test_connection_drop_is_retried_and_keeps_the_ping(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)

    class Response:
        status_code, ok = 200, True

    class Session:
        headers = {}
        calls = []

        def post(self, url, json, timeout):
            self.calls.append(json)
            if len(self.calls) < 3:
                raise bot.requests.ConnectionError("dropped")
            return Response()

    session = Session()
    bot.Discord("https://example.test", "123", session).send([{"title": "VIP in game"}], ping=True)
    assert len(session.calls) == 3
    assert session.calls[-1]["content"] == "<@&123>"


def test_join_alert_links_the_game_page(tmp_path):
    roblox, tracker = make(tmp_path)
    roblox.now[2] = in_game()
    embed, _ = tracker.poll(2000)[0]
    travel = [f for f in embed["fields"] if f["name"] == "Game"][0]["value"]
    assert travel == "[Travel](https://www.roblox.com/games/4238077359/Star-Wars-Roleplay)"


class Sent:
    def __init__(self):
        self.embeds = []

    def send(self, embeds, ping=False):
        self.embeds += embeds

    def titles(self):
        return [embed["title"] for embed in self.embeds]


def stopping_watch(error):
    def fake_watch(*args):
        raise error
    return fake_watch


def test_stopping_posts_offline_and_is_not_reported_again(tmp_path, monkeypatch):
    roblox, tracker = make(tmp_path)
    discord = Sent()
    monkeypatch.setattr(bot, "watch", stopping_watch(KeyboardInterrupt()))
    bot.run(tracker, roblox, discord, tracker.settings)
    assert discord.titles() == ["VIP tracker offline"]
    monkeypatch.setattr(bot.time, "time", lambda: 9_999_999_999)
    assert bot.Tracker(roblox, None, CONFIG, tracker.settings).unnoticed_downtime() is None


def test_crash_posts_offline_and_still_raises(tmp_path, monkeypatch):
    roblox, tracker = make(tmp_path)
    discord = Sent()
    monkeypatch.setattr(bot, "watch", stopping_watch(ValueError("boom")))
    try:
        bot.run(tracker, roblox, discord, tracker.settings)
        raise AssertionError("crash was swallowed")
    except ValueError:
        pass
    assert "ValueError" in discord.embeds[0]["description"]


def test_power_loss_is_reported_on_next_start(tmp_path, monkeypatch):
    roblox, tracker = make(tmp_path)
    alive = tracker.state["_last_alive"]
    monkeypatch.setattr(bot.time, "time", lambda: alive + 600)
    restarted = bot.Tracker(roblox, None, CONFIG, tracker.settings)
    assert restarted.unnoticed_downtime() == alive


def test_quick_restart_is_not_reported(tmp_path):
    roblox, tracker = make(tmp_path)
    assert bot.Tracker(roblox, None, CONFIG, tracker.settings).unnoticed_downtime() is None


def test_vips_lists_highest_killzone_first(tmp_path):
    from slash_commands import vips_embed
    roblox, tracker = make(tmp_path)
    assert "Nobody" in vips_embed(tracker)["description"]
    roblox.now[2] = in_game()
    roblox.now[8] = in_game()
    tracker.poll(2000)
    embed = vips_embed(tracker)
    assert embed["title"] == "VIPs in game: 2"
    assert embed["description"].startswith("**Chancellor** | Supreme Chancellor (CG) | 12 studs\n")


def test_vips_while_starting(tmp_path):
    from slash_commands import vips_embed
    settings = {"state_file": str(tmp_path / "state.json")}
    tracker = bot.Tracker(FakeRoblox(), None, CONFIG, settings)
    assert "still starting" in vips_embed(tracker)["description"]
