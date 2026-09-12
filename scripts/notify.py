"""Report from an unattended job to every configured notification channel.

    echo "index rebuilt, all green" | uv run student-bot-notify
    uv run student-bot-notify --severity critical --message 'recall@5 fell'
    uv run student-bot-notify --to '#bot-ops' --message 'hello'
    uv run student-bot-notify -n --severity warn --message 'check'   # resolve only

Channels are configured under `notify:` in config.yaml and fire in parallel —
there is no primary and no fallback. Two are supported:

  * **Mattermost**, posted as the bot account. The credentials are already in
    `.env` and already work, so there is no new secret to provision or rotate,
    and the message lands where the bot's other conversations are. This is a
    separate entry point from the bot itself on purpose: the weekly job has to
    be able to report *that the bot is broken*, which rules out routing the
    report through the bot's own websocket loop.

  * **ntfy**, an HTTP POST to an ntfy.sh-compatible server. A Mattermost DM at
    04:00 sits unread until morning; ntfy pushes to a phone. Severity maps to
    ntfy's priority, so a recall drop can ring through a silenced phone while a
    green week cannot.

WHAT MAY BE SENT
    The maintenance report is built from `eval/eval_set.yml` (checked into the
    repo) plus chunk counts — no `qa_log` text — which is what makes it safe to
    hand to a third-party push service. If a report ever starts quoting real
    student questions, ntfy must move to a self-hosted server or go.

A channel that is not configured is skipped silently; a channel that is
configured and FAILS is reported on stderr and sets the exit code. If no
channel is configured at all, that is an error: a job that thinks it notified
someone and did not is the failure mode this whole path exists to avoid.
"""

from __future__ import annotations

import sys

import click
import httpx
from mattermostdriver import Driver

from student_bot.config import Config, get_config


# Mattermost rejects posts above 16383 characters. Reports should never come
# close, but a runaway traceback pasted into one would otherwise turn a useful
# alert into a silent API error.
MAX_MESSAGE = 16000

SEVERITIES = ("ok", "warn", "critical")

# ntfy priorities: 1 min, 3 default, 5 max (bypasses a silenced phone).
# "ok" is deliberately BELOW default: a weekly all-green push that buzzes is
# one people learn to swipe away, which costs you the alert that matters.
_NTFY = {
    "ok": {"priority": "2", "tags": "white_check_mark", "title": "student-bot: maintenance OK"},
    "warn": {"priority": "4", "tags": "warning", "title": "student-bot: check the weekly run"},
    "critical": {"priority": "5", "tags": "rotating_light", "title": "student-bot: recall dropped"},
}


def severity_at_least(severity: str, minimum: str) -> bool:
    """Is `severity` at or above the configured floor? Unknown values are
    treated as the most severe — a typo in config.yaml must not silence an
    alert."""
    try:
        return SEVERITIES.index(severity) >= SEVERITIES.index(minimum)
    except ValueError:
        return True


def _driver(cfg: Config) -> Driver:
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
            "No notification target. Pass --to, or set notify.mattermost_target "
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


def ntfy_request(cfg: Config, message: str, severity: str) -> tuple[str, dict[str, str], bytes]:
    """Build the (url, headers, body) for one ntfy publish.

    Split out from the POST so the wiring is testable without a network: the
    parts that are easy to get wrong — which header carries the title, whether
    the topic ends up in the URL, that the body is UTF-8 — are all here.
    """
    topic = cfg.ntfy_topic.get_secret_value() if cfg.ntfy_topic else ""
    if not topic:
        raise SystemExit("ntfy: NTFY_TOPIC is not set")
    url = f"{cfg.notify.ntfy_server.rstrip('/')}/{topic.lstrip('/')}"
    meta = _NTFY.get(severity, _NTFY["critical"])
    headers = {
        "Title": meta["title"],
        "Priority": meta["priority"],
        "Tags": meta["tags"],
        # ntfy renders Markdown only when asked; without this the report's
        # table arrives as a wall of pipes.
        "Markdown": "yes",
    }
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token.get_secret_value()}"
    return url, headers, message.encode("utf-8")


def send_ntfy(cfg: Config, message: str, severity: str) -> None:
    url, headers, body = ntfy_request(cfg, message, severity)
    response = httpx.post(
        url, headers=headers, content=body, timeout=cfg.notify.ntfy_timeout_seconds
    )
    response.raise_for_status()


def send_mattermost(cfg: Config, message: str, target: str) -> None:
    driver = _driver(cfg)
    driver.login()
    assert cfg.mattermost_secrets is not None
    channel_id = resolve_channel_id(driver, target, team=cfg.mattermost_secrets.team)
    driver.posts.create_post({"channel_id": channel_id, "message": message})


@click.command()
@click.option("--to", "target", default=None, help="'@user' or '#channel'. Overrides config.")
@click.option("--message", default=None, help="Message text. Omit to read stdin.")
@click.option(
    "--severity",
    type=click.Choice(SEVERITIES),
    default="ok",
    show_default=True,
    help="Drives ntfy priority, and whether a channel fires at all.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Resolve the targets and print, don't send.")
def main(target: str | None, message: str | None, severity: str, dry_run: bool):
    cfg = get_config()

    if message is None:
        message = sys.stdin.read()
    message = message.strip()
    if not message:
        raise SystemExit("Refusing to send an empty message.")
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + "\n\n_…truncated._"

    mm_target = target if target is not None else cfg.notify.mattermost_target
    has_ntfy = bool(cfg.ntfy_topic and cfg.ntfy_topic.get_secret_value())

    if not mm_target and not has_ntfy:
        raise SystemExit(
            "No notification channel is configured. Set notify.mattermost_target "
            "in config.yaml, or NTFY_TOPIC in .env."
        )

    # An explicit --to is an instruction, not a preference: honour it even
    # below the configured floor. The floor exists to keep routine traffic off
    # a phone, not to override a human asking for a message right now.
    floor = cfg.notify.min_severity
    send_anyway = target is not None
    if not send_anyway and not severity_at_least(severity, floor):
        click.echo(f"[notify] severity '{severity}' is below min_severity '{floor}'; nothing sent")
        return

    if dry_run:
        click.echo(f"[notify] severity={severity} (floor={floor})")
        click.echo(f"[notify] mattermost: {mm_target or '<not configured>'}")
        click.echo(
            f"[notify] ntfy:       {ntfy_request(cfg, message, severity)[0]}"
            if has_ntfy
            else "[notify] ntfy:       <not configured>"
        )
        click.echo(message)
        return

    # Each channel is attempted independently: one being down is exactly when
    # the other earns its place.
    failures: list[str] = []
    if mm_target:
        try:
            send_mattermost(cfg, message, mm_target)
            click.echo(f"[notify] mattermost -> {mm_target}")
        except Exception as e:
            failures.append(f"mattermost: {e}")
    if has_ntfy:
        try:
            send_ntfy(cfg, message, severity)
            click.echo(f"[notify] ntfy -> {cfg.notify.ntfy_server}")
        except Exception as e:
            failures.append(f"ntfy: {e}")

    for failure in failures:
        click.echo(f"[notify] FAILED {failure}", err=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
