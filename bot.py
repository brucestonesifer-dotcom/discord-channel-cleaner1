import os
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord import app_commands

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

# =========================
# CONFIG
# =========================
TOKEN = "MTQ4MzMwMjQzMzM5OTY0MDIwNA.G6rCzx.QE5hJnELfF5PdQkbRaC-Nw4A2hcmOMcyo7XLiw"
TIMEZONE = "America/Chicago"
DATA_FILE = "scheduled_deletions.json"

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# =========================
# STORAGE HELPERS
# =========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def add_job_record(record):
    data = load_data()
    data.append(record)
    save_data(data)


def remove_job_record(job_id):
    data = load_data()
    data = [x for x in data if x["job_id"] != job_id]
    save_data(data)


def get_job_record(job_id):
    data = load_data()
    for item in data:
        if item["job_id"] == job_id:
            return item
    return None


# =========================
# DELETE LOGIC
# =========================
async def delete_channel_job(guild_id: int, channel_id: int, job_id: str):
    guild = bot.get_guild(guild_id)
    if guild is None:
        remove_job_record(job_id)
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        remove_job_record(job_id)
        return

    try:
        await channel.delete(reason=f"Scheduled deletion (job {job_id})")
    except discord.Forbidden:
        print(f"[ERROR] Missing permission to delete channel {channel_id} in guild {guild_id}")
    except discord.HTTPException as e:
        print(f"[ERROR] Failed deleting channel {channel_id}: {e}")
    finally:
        # For one-time jobs, remove after attempt.
        record = get_job_record(job_id)
        if record and record.get("type") == "once":
            remove_job_record(job_id)


# =========================
# RELOAD SAVED JOBS ON START
# =========================
def restore_jobs():
    data = load_data()
    for record in data:
        job_id = record["job_id"]
        guild_id = record["guild_id"]
        channel_id = record["channel_id"]

        if record["type"] == "once":
            run_at = datetime.fromisoformat(record["run_at"])
            if run_at > datetime.now(ZoneInfo(TIMEZONE)):
                scheduler.add_job(
                    delete_channel_job,
                    trigger=DateTrigger(run_date=run_at),
                    args=[guild_id, channel_id, job_id],
                    id=job_id,
                    replace_existing=True,
                )
        elif record["type"] == "recurring":
            scheduler.add_job(
                delete_channel_job,
                trigger=CronTrigger(
                    hour=record["hour"],
                    minute=record["minute"],
                    timezone=TIMEZONE,
                ),
                args=[guild_id, channel_id, job_id],
                id=job_id,
                replace_existing=True,
            )


# =========================
# PERMISSION CHECK
# =========================
def is_admin():
    async def predicate(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            raise app_commands.CheckFailure("You must be an administrator to use this command.")
        return True
    return app_commands.check(predicate)


# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="schedule_delete_once", description="Delete a channel once at a specific date and time.")
@is_admin()
@app_commands.describe(
    channel="Channel to delete",
    date="Date in YYYY-MM-DD format",
    time="Time in HH:MM 24-hour format"
)
async def schedule_delete_once(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    date: str,
    time: str
):
    try:
        run_at = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=ZoneInfo(TIMEZONE))
    except ValueError:
        await interaction.response.send_message(
            "Invalid date/time format. Use YYYY-MM-DD and HH:MM.",
            ephemeral=True
        )
        return

    if run_at <= datetime.now(ZoneInfo(TIMEZONE)):
        await interaction.response.send_message(
            "That date/time is in the past.",
            ephemeral=True
        )
        return

    job_id = f"once_{interaction.guild_id}_{channel.id}_{int(run_at.timestamp())}"

    scheduler.add_job(
        delete_channel_job,
        trigger=DateTrigger(run_date=run_at),
        args=[interaction.guild_id, channel.id, job_id],
        id=job_id,
        replace_existing=True,
    )

    add_job_record({
        "job_id": job_id,
        "type": "once",
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "run_at": run_at.isoformat(),
    })

    await interaction.response.send_message(
        f"Scheduled {channel.mention} for deletion on **{run_at.strftime('%Y-%m-%d %H:%M %Z')}**."
    )


@bot.tree.command(name="schedule_delete_daily", description="Delete a channel every day at a set time.")
@is_admin()
@app_commands.describe(
    channel="Channel to delete",
    hour="Hour (0-23)",
    minute="Minute (0-59)"
)
async def schedule_delete_daily(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    hour: app_commands.Range[int, 0, 23],
    minute: app_commands.Range[int, 0, 59]
):
    job_id = f"daily_{interaction.guild_id}_{channel.id}_{hour}_{minute}"

    scheduler.add_job(
        delete_channel_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
        args=[interaction.guild_id, channel.id, job_id],
        id=job_id,
        replace_existing=True,
    )

    # Replace saved record if it already exists
    data = load_data()
    data = [x for x in data if x["job_id"] != job_id]
    data.append({
        "job_id": job_id,
        "type": "recurring",
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "hour": hour,
        "minute": minute,
    })
    save_data(data)

    await interaction.response.send_message(
        f"Scheduled {channel.mention} for deletion every day at **{hour:02d}:{minute:02d} {TIMEZONE}**."
    )


@bot.tree.command(name="list_delete_jobs", description="List scheduled channel deletion jobs.")
@is_admin()
async def list_delete_jobs(interaction: discord.Interaction):
    data = [x for x in load_data() if x["guild_id"] == interaction.guild_id]

    if not data:
        await interaction.response.send_message("No scheduled deletion jobs found.", ephemeral=True)
        return

    lines = []
    for item in data:
        channel = interaction.guild.get_channel(item["channel_id"])
        channel_name = channel.mention if channel else f"`deleted-channel:{item['channel_id']}`"

        if item["type"] == "once":
            lines.append(f"**{item['job_id']}** — {channel_name} — once at `{item['run_at']}`")
        else:
            lines.append(f"**{item['job_id']}** — {channel_name} — daily at `{item['hour']:02d}:{item['minute']:02d}`")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="cancel_delete_job", description="Cancel a scheduled channel deletion job.")
@is_admin()
@app_commands.describe(job_id="The job ID shown by /list_delete_jobs")
async def cancel_delete_job(interaction: discord.Interaction, job_id: str):
    job = scheduler.get_job(job_id)
    if not job:
        await interaction.response.send_message("Job not found.", ephemeral=True)
        return

    scheduler.remove_job(job_id)
    remove_job_record(job_id)

    await interaction.response.send_message(f"Cancelled job `{job_id}`.", ephemeral=True)


# =========================
# ERROR HANDLING
# =========================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send(str(error), ephemeral=True)
        else:
            await interaction.response.send_message(str(error), ephemeral=True)
    else:
        if interaction.response.is_done():
            await interaction.followup.send(f"Error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)


# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    restore_jobs()
    if not scheduler.running:
        scheduler.start()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


bot.run(TOKEN)