"""Optional /vips command. Only runs when DISCORD_BOT_TOKEN is set."""
import asyncio
import logging
import threading

log = logging.getLogger("vip-tracker")


def vips_embed(tracker):
    """Builds the /vips reply from what the bot last saw."""
    last_check = tracker.state.get("_last_alive")
    if not tracker.roster or not last_check:
        return {"title": "VIPs in game", "description": "The bot is still starting. Try again shortly.",
                "color": 0x607D8B}
    vips = sorted(tracker.in_game(), key=lambda vip: (-vip.studs, vip.rank, vip.username.casefold()))
    lines = [f"**{vip.username}** | {vip.rank} ({vip.group}) | {vip.studs} studs" for vip in vips]
    summary = f"Last checked <t:{int(last_check)}:R>. VIPs with hidden privacy settings don't appear."
    return {
        "title": f"VIPs in game: {len(vips)}",
        "description": "\n".join(lines + ["", summary]) if lines else f"Nobody right now.\n\n{summary}",
        "color": 0x2E7D32 if vips else 0x607D8B,
    }


def build_client(tracker):
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    @tree.command(name="vips", description="See which VIPs are in game right now")
    async def vips(interaction):
        await interaction.response.send_message(
            embed=discord.Embed.from_dict(vips_embed(tracker)), ephemeral=True
        )

    synced = []

    @client.event
    async def on_ready():
        if synced:
            return
        synced.append(True)
        log.info("Discord bot logged in as %s, in %s server(s)", client.user, len(client.guilds))
        if not client.guilds:
            log.error("/vips: the bot is not in any server, so invite it first")
        # Per-server commands appear straight away; global ones can lag.
        for guild in client.guilds:
            try:
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
                log.info("/vips command is ready in %s", guild.name)
            except discord.DiscordException as error:
                log.error("/vips could not be added to %s: %s. Re-invite the bot with "
                          "the applications.commands box ticked.", guild.name, error)
        tree.clear_commands(guild=None)
        try:
            await tree.sync()  # Removes the old global copy so /vips isn't listed twice.
        except discord.DiscordException as error:
            log.warning("Could not clear the global /vips: %s", error)

    return client


def start_commands(tracker, token):
    """Runs beside the tracker, so a Discord login problem can't stop the alerts."""

    def runner():
        try:
            asyncio.run(build_client(tracker).start(token))
        except Exception as error:
            log.error("/vips is unavailable: %s", type(error).__name__)

    threading.Thread(target=runner, daemon=True).start()
