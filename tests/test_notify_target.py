"""Target resolution for scripts/notify.py.

The only caller is an unattended cron job, so a misconfigured target has to
fail with a sentence someone can act on from a log file — never with a driver
traceback, and never by silently posting somewhere unintended.
"""

from __future__ import annotations

import pytest

from scripts.notify import resolve_channel_id


class FakeUsers:
    def __init__(self, known: dict[str, str]):
        self._known = known

    def get_user_by_username(self, username: str) -> dict:
        if username not in self._known:
            raise RuntimeError("404 not found")
        return {"id": self._known[username]}

    def get_user(self, user_id: str) -> dict:
        return {"id": "bot-self"}


class FakeChannels:
    def __init__(self, channels: dict[str, str]):
        self._channels = channels
        self.dm_members: list[str] | None = None

    def create_direct_message_channel(self, members: list[str]) -> dict:
        self.dm_members = members
        return {"id": "dm-channel"}

    def get_channel_by_name(self, team_id: str, name: str) -> dict:
        if name not in self._channels:
            raise RuntimeError("404 not found")
        return {"id": self._channels[name]}


class FakeTeams:
    def __init__(self, teams: dict[str, str]):
        self._teams = teams

    def get_team_by_name(self, name: str) -> dict:
        if name not in self._teams:
            raise RuntimeError("404 not found")
        return {"id": self._teams[name]}


class FakeDriver:
    def __init__(self, *, users=None, channels=None, teams=None):
        self.users = FakeUsers(users or {"chohm": "user-1"})
        self.channels = FakeChannels(channels or {"bot-ops": "chan-1"})
        self.teams = FakeTeams(teams or {"kth": "team-1"})


def test_at_prefix_opens_a_direct_message():
    d = FakeDriver()
    assert resolve_channel_id(d, "@chohm", team="kth") == "dm-channel"
    assert sorted(d.channels.dm_members) == ["bot-self", "user-1"]


def test_a_dm_does_not_need_a_team():
    """DMs are team-independent, so requiring MATTERMOST_TEAM for one would be
    an invented obstacle on a host that only ever sends DMs."""
    assert resolve_channel_id(FakeDriver(), "@chohm", team=None) == "dm-channel"


def test_hash_prefix_resolves_a_channel():
    assert resolve_channel_id(FakeDriver(), "#bot-ops", team="kth") == "chan-1"


def test_bare_name_is_treated_as_a_channel():
    assert resolve_channel_id(FakeDriver(), "bot-ops", team="kth") == "chan-1"


def test_surrounding_whitespace_is_tolerated():
    assert resolve_channel_id(FakeDriver(), "  @chohm \n", team="kth") == "dm-channel"


def test_empty_target_names_the_config_key_to_set():
    with pytest.raises(SystemExit) as e:
        resolve_channel_id(FakeDriver(), "", team="kth")
    assert "notify_target" in str(e.value)


def test_channel_without_a_team_explains_both_ways_out():
    with pytest.raises(SystemExit) as e:
        resolve_channel_id(FakeDriver(), "#bot-ops", team=None)
    message = str(e.value)
    assert "MATTERMOST_TEAM" in message
    assert "@username" in message


def test_unknown_user_names_the_user():
    with pytest.raises(SystemExit) as e:
        resolve_channel_id(FakeDriver(), "@nobody", team="kth")
    assert "@nobody" in str(e.value)


def test_unknown_channel_names_the_channel_and_team():
    with pytest.raises(SystemExit) as e:
        resolve_channel_id(FakeDriver(), "#nowhere", team="kth")
    assert "nowhere" in str(e.value)
    assert "kth" in str(e.value)
