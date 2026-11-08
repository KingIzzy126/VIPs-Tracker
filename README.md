# Coruscant Guard VIP tracker

Posts to Discord when a VIP joins or leaves Star Wars: Roleplay. Each alert shows
their rank, killzone, a link to their Roblox profile and a link to the game. VIP lists refresh every 30 minutes,
so promotions and demotions are picked up automatically.

## Setup

1. Install Python 3.11 or newer.
2. Create a Discord webhook in `#vip-call-outs`, and copy the VIP Ping role ID.
3. Create a separate Roblox alt account for the bot. Set your own account aside.
4. Log in to the alt in a private browser window. Open DevTools > Application > Cookies >
   roblox.com and copy the `.ROBLOSECURITY` value. Close the window without logging out,
   because logging out kills the cookie.
5. Copy `.env.example` to `.env` and fill in the three values.
6. Install and check:

   ```
   pip install -r requirements.txt
   python bot.py --check
   ```

   `--check` posts nothing. It lists every VIP found with their killzone, any roles it
   could not match, and how many VIPs are in game right now.
7. Start it with `python bot.py`. A "VIP tracker online" message confirms it is working.

## Keeping it online

The bot only runs while its computer is on. For 24/7 alerts, put it on a small always-on
host and enter the `.env` values in the host's environment settings. Never upload `.env`.
`Procfile` tells hosts such as Railway what to run; use a worker service, not a web one.

## Security

- The cookie gives full control of that Roblox account. Treat it like a password.
- If it leaks, log the alt out of all sessions to kill it.
- Anyone with the webhook URL can post in the channel. If it leaks, delete and recreate it.

## Changing VIP ranks

Edit `config.json`. Role names must match Roblox exactly, apart from capital letters.

To track someone who isn't in a listed rank, add them under `extra_vips`. `user_id` is the
number in their Roblox profile link, and an entry with `user_id` 0 is ignored.

The GAR `Owner` rank is shown as Supreme Chancellor through `display_rank`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `POLL_SECONDS` | 30 | Time between checks |
| `OFFLINE_POLLS` | 4 | Missed checks in a row before a "left" alert |
| `PING_COOLDOWN_MINUTES` | 15 | Minimum gap between pings for one VIP |
| `POST_HIDDEN` | false | Also post, without a ping, when a VIP is in a game Roblox hides |

## Known limits

- Roblox only reveals a player's game if that player's privacy settings allow it for the
  bot's account. Hidden VIPs get no alert. Adding the alt as a friend can help.
- The bot cannot see morphs, teams or AFK status, so guards must confirm in game.
- Roblox presence is an older, unsupported API and could change without notice.

## The /vips command

`/vips` shows which VIPs are in game, and only the person who used it can see the reply.

1. At discord.com/developers/applications, click New Application and name it.
2. Open Bot, click Reset Token, and copy the token. Treat it like a password.
3. Open OAuth2 > URL Generator, tick `bot` and `applications.commands`, then open the link
   at the bottom and add the bot to your server. It needs no extra permissions.
4. Add `DISCORD_BOT_TOKEN=your token` to `.env`, or to the host's variables, and restart.

After adding the bot to another server, restart it so `/vips` is added there too.

## Offline notices

The bot posts "VIP tracker offline" when it is stopped, closed or crashes. If the computer
loses power or sleeps, it cannot post at that moment, so it posts "VIP tracker was offline"
once it is running again. Repeated Roblox failures post a warning after a few minutes.

## Tests

```
pip install pytest
python -m pytest test_bot.py
```
