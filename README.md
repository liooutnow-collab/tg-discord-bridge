# Telegram → Discord Bridge

A lightweight Python background worker that watches Telegram groups your Telegram account can access and posts only messages matching configured filters to a Discord webhook.

## Important constraints

- This uses a normal Telegram user session through Telethon (MTProto), not a bot added to the group.
- The Telegram account must already be allowed to view the public group. It cannot bypass private-group membership, bans, access controls, or Telegram restrictions.
- Use this only for groups and content you are authorized to monitor and redistribute. Respect group rules, privacy, copyright, and platform terms.
- Never commit API credentials, a Telethon session string, or a Discord webhook URL. The repository may be public; put secrets only in the hosting provider's encrypted environment-variable settings.
- Continuous operation depends on the hosting provider's worker availability, storage persistence, network access, and free-tier limits.

## Required environment variables

| Variable | Meaning |
|---|---|
| `TELEGRAM_API_ID` | Numeric API ID from https://my.telegram.org |
| `TELEGRAM_API_HASH` | Telegram API hash from https://my.telegram.org |
| `TELEGRAM_SESSION` | Telethon StringSession for the account that can access the group |
| `TELEGRAM_SOURCE_CHATS` | Comma-separated public usernames or numeric chat IDs |
| `DISCORD_WEBHOOK_URL` | Webhook URL for the destination Discord channel |
| `FILTER_KEYWORDS` | Comma-separated case-insensitive phrases |
| `FILTER_USERS` | Comma-separated Telegram usernames, without @ |
| `FILTER_REGEX` | Comma-separated regular expressions (avoid commas inside patterns) |

At least one of FILTER_KEYWORDS, FILTER_USERS, or FILTER_REGEX must be set. If no filter is set, startup fails rather than forwarding every message.

Optional:
- `FILTER_MODE=any` (default): match any configured filter category.
- `FILTER_MODE=all`: every configured filter category must match.
- `EXCLUDE_KEYWORDS`: messages containing any excluded phrase are rejected.
- `DATABASE_PATH=/app/data/bridge_state.sqlite3`: SQLite deduplication database.
- `LOG_LEVEL=INFO`.
- `FORWARD_EDITED_MESSAGES=false`: edit forwarding is not implemented; leave false.

## Deploy on Deplexo

1. Import this repository.
2. Choose **Background worker**, not Web service.
3. Choose **Dockerfile** build.
4. Set Dockerfile path to `Dockerfile`.
5. Leave the start command blank so the Dockerfile CMD runs.
6. Add the required environment variables in the provider's encrypted environment-variable panel.
7. If Deplexo offers persistent volumes, mount one at `/app/data`. Without persistent storage, duplicate protection may reset when the container is recreated.
8. Deploy and inspect logs. A successful startup should show “Telegram connected” and “Bridge is running”.

## Message behavior

- New messages in the configured group(s) are evaluated.
- Excluded phrases take precedence.
- Matching text is posted in a Discord embed with source attribution and a Telegram link when a public username link is available.
- Messages are not marked delivered until Discord confirms success.
- SQLite prevents the same chat/message pair from being sent twice while its database persists.
- This initial version forwards text/captions and links, not the actual media file. It does not backfill old messages or forward edits.

## Security

Treat TELEGRAM_SESSION as equivalent to account access. Anyone who obtains it may be able to act as the Telegram account. Use a dedicated account where appropriate, restrict repository access if possible, never paste secrets into issues or chat, and revoke the session from Telegram active sessions if it is exposed. Rotate a leaked Discord webhook immediately.
