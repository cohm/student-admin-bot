"""Post a message to Mattermost as the bot — for unattended jobs to report in.

    echo "index rebuilt, all green" | uv run student-bot-notify
    uv run student-bot-notify --to '@chohm' --message 'hello'
    uv run student-bot-notify --to '#bot-ops' --message 'hello'

The target comes from `--to`, else `mattermost.notify_target` in config.yaml.
"@name" is a direct message; "#name" (or a bare name) is a channel on
`MATTERMOST_TEAM`.

WHY REUSE THE BOT ACCOUNT RATHER THAN AN INCOMING WEBHOOK
    The credentials are already in `.env` and already work, so there is no new
    secret to provision, rotate or leak, and a DM from the bot lands in the
    same place students' conversations with it do. An incoming webhook would
    be one more URL to manage for no extra capability.

This is deliberately a separate entry point from the bot itself: the weekly
maintenance job must be able to report *that the bot is broken*, which rules
out routing the report through the bot's own websocket loop.
"""

from __future__ import annotations

import sys

import click
from mattermostdriver import Driver

from student_bot.config import get_config


# Mattermost rejects posts above 16383 characters. Reports should never come
# close, but a runaway traceback pasted into one would otherwise turn a useful
# alert into a silent API error.
MAX_MESSAGE = 16000


def _driver(cfg) -> Driver:
    s = cfg.mattermost_secrets
    if s is None:
        raise SystemExit(
            "Mattermost credentials are not configured. Set MATTERMOST_URL and "
            "MATTERMOST_TOKEN in .env (see .env.example)."
        )
    return Driver(
        {
            "url": s.url,
            "token": s.token,
            "scheme": s.scheme,
            "port": s.port,
            "basepath": "/api/v4",
            "verify": True,
            "timeout": 30,
        }
    )


def resolve_channel_id(driver: Driver, target: str, *, team: str | None) -> str:
    """Turn "@user" / "#channel" / "channel" into a channel id.

    Raises SystemExit with an actionable message rather than a driver
    exception, because the only caller is a cron job whose log is the only
    place anyone will read the failure.
    """
    target = target.strip()
    if not target:
        raise SystemExit(
            "No notification target. Pass --to, or set mattermost.notify_target "
            "in config.yaml (e.g. '@chohm')."
        )

    if target.startswith("@"):
        username = target[1:]
        try:
            other = driver.users.get_user_by_username(username)
        except Exception as e:
            raise SystemExit(f"No Mattermost user '@{username}': {e}") from e
        me = driver.users.get_user(user_id="me")
        channel = driver.channels.create_direct_message_channel([me["id"], other["id"]])
        return channel["id"]

    name = target[1:] if target.startswith("#") else target
    if not team:
        raise SystemExit(
            f"Posting to channel '{name}' needs a team: set MATTERMOST_TEAM in .env, "
            "or use an '@username' target instead (DMs need no team)."
        )
    try:
        team_obj = driver.teams.get_team_by_name(team)
        channel = driver.channels.get_channel_by_name(team_obj["id"], name)
    except Exception as e:
        raise SystemExit(f"No channel '{name}' on team '{team}': {e}") from e
    return channel["id"]


@click.command()
@click.option("--to", "target", default=None, help="'@user' or '#channel'. Overrides config.")
@click.option("--message", default=None, help="Message text. Omit to read stdin.")
@click.option("-n", "--dry-run", is_flag=True, help="Resolve the target and print, don't post.")
def main(target: str | None, message: str | None, dry_run: bool):
    cfg = get_config()

    if message is None:
        message = sys.stdin.read()
    message = message.strip()
    if not message:
        raise SystemExit("Refusing to post an empty message.")
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + "\n\n_…truncated._"

    resolved = target if target is not None else cfg.mattermost.notify_target

    if dry_run:
        click.echo(f"[notify] would post to {resolved!r}:\n{message}")
        return

    driver = _driver(cfg)  # raises if credentials are missing
    driver.login()
    assert cfg.mattermost_secrets is not None
    channel_id = resolve_channel_id(driver, resolved, team=cfg.mattermost_secrets.team)
    driver.posts.create_post({"channel_id": channel_id, "message": message})
    click.echo(f"[notify] posted to {resolved}")


if __name__ == "__main__":
    main()
