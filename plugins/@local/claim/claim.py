import re
import unicodedata

import discord
from discord.ext import commands
from discord.utils import utcnow
from motor import motor_asyncio

from core import checks
from core.models import PermissionLevel, getLogger

from .core.config import Config

from bot import ModmailBot

logger = getLogger(__name__)


def _channel_slug(value: str) -> str:
    """Convert a Discord username into a clean channel-name component."""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "user"


async def claim_check(ctx):
    if ctx.author.bot:
        return True
    if await ctx.bot.is_owner(ctx.author):
        return True
    cog = ctx.bot.get_cog("Claim")
    thread_data = await cog.db.find_one({"channel_id": str(ctx.thread.channel.id)})
    allowed_to_reply = False
    if thread_data is None:
        if cog.config["require_claim"] is False:
            allowed_to_reply = True
    else:
        if str(ctx.author.id) in thread_data["claimers"]:
            allowed_to_reply = True
    return allowed_to_reply


claim_check.fail_msg = (
    "You must claim this thread before you can reply to it. "
    "Use the `claim` command to claim it, or ask its current claimer(s) to add you."
)


class Claim(commands.Cog):
    """
    Adds claim functionality to your modmail bot.

    Only members claimed a thread can reply to the thread via the reply commands.
    """

    def __init__(self, bot: ModmailBot):
        self.bot: ModmailBot = bot
        self.db: motor_asyncio.AsyncIOMotorCollection = bot.api.get_plugin_partition(self)
        self.reply_commands = ["reply", "areply", "freply", "fareply", "fareply", "preply", "pareply"]
        self.config = Config(self.db, self.bot)
        self.initialized = False

    async def cog_load(self):
        """
        Verifies plugin config, adds the claim check on cog load/plugin installation.
        """
        if not self.initialized:
            await self.config.fetch()

            for i in self.reply_commands:
                cmd = self.bot.get_command(i)
                if not claim_check in cmd.checks:
                    cmd.add_check(claim_check)
            self.initialized = True

    async def cog_unload(self):
        """
        Removes the claim check on cog unload/plugin removal.
        """
        self.initialized = False
        for i in self.reply_commands:
            cmd = self.bot.get_command(i)
            if claim_check in cmd.checks:
                cmd.remove_check(claim_check)

    @commands.command(name="claim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.max_concurrency(number=1, per=commands.BucketType.channel, wait=False)
    async def claim(self, ctx: commands.Context):
        """
        Claims thread on behalf of you.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            await self.db.insert_one(
                {
                    "channel_id": str(ctx.thread.channel.id),
                    "main_claimer": str(ctx.author.id),
                    "claimed_at": utcnow(),
                    "claimers": [str(ctx.author.id)],
                    "original_channel_name": ctx.thread.channel.name,
                }
            )
            recipient = ctx.thread.recipient
            recipient_name = recipient.name if recipient is not None else str(ctx.thread.id)
            new_channel_name = (
                f"{_channel_slug(ctx.author.name)}-{_channel_slug(recipient_name)}"[:100].rstrip("-")
            )
            try:
                await ctx.thread.channel.edit(
                    name=new_channel_name,
                    reason=f"Ticket claimed by {ctx.author}",
                )
            except discord.HTTPException:
                logger.warning("Failed to rename claimed thread %s.", ctx.thread.channel.id, exc_info=True)

            embed = discord.Embed(
                title="Thread claimed",
                description=f"{ctx.author.mention} successfully claimed this thread.",
                color=ctx.bot.main_color,
            )
            # Mentions inside embeds do not create notifications, so send the
            # claimant mention as message content as well to guarantee a ping.
            return await ctx.send(
                content=ctx.author.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[ctx.author],
                    roles=False,
                    everyone=False,
                ),
            )
        else:
            claimers_mentions = [f"<@{i}>" for i in thread_data["claimers"]]
            claimers_mentions_str = ", ".join(claimers_mentions)
            embed = discord.Embed(
                title="Thread already claimed",
                description=f"This thread is already claimed by {claimers_mentions_str}.",
                color=ctx.bot.error_color,
            )
            await ctx.send(embed=embed)

    @commands.command(name="addclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def addclaim(self, ctx: commands.Context, *, member: discord.Member):
        """
        Adds another member to your claim.

        This allows all manually added members to reply in threads.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if not str(ctx.author.id) == thread_data["main_claimer"]:
            embed = discord.Embed(
                title="Thread not claimed by you.",
                description=f"You have not claimed this thread. Ask <@{thread_data['main_claimer']}> to add you as claimer.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if str(member.id) in thread_data["claimers"]:
            embed = discord.Embed(
                title="Member already added",
                description=f"The member {member.mention} is already added to the claimers.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        thread_data["claimers"].append(str(member.id))
        await self.db.update_one({"channel_id": str(ctx.thread.channel.id)}, {"$set": thread_data})
        embed = discord.Embed(
            title="Member added",
            description=f"You successfully added {member.mention} to the claimers.",
            color=ctx.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="removeclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def removeclaim(self, ctx: commands.Context, *, member: discord.Member):
        """
        Removes a member from your claim.

        This removes an added member from the thread claimers. They can no longer reply.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if not str(ctx.author.id) == thread_data["main_claimer"]:
            embed = discord.Embed(
                title="Thread not claimed by you.",
                description=f"You have not claimed this thread. Ask <@{thread_data['main_claimer']}> remove you from the claimers.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)

        if not str(member.id) in thread_data["claimers"]:
            embed = discord.Embed(
                title="Member not added",
                description=f"The member {member.mention} is not added to the claimers.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if str(member.id) == str(ctx.author.id):
            embed = discord.Embed(
                title="You cannot remove yourself",
                description=f"You cannot remove yourself from the claimers. Unclaim the thread instead.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        thread_data["claimers"].remove(str(member.id))
        await self.db.update_one({"channel_id": str(ctx.thread.channel.id)}, {"$set": thread_data})
        embed = discord.Embed(
            title="Member removed",
            description=f"You successfully removed {member.mention} from the claimers.",
            color=ctx.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="unclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unclaim(self, ctx: commands.Context):
        """
        Unclaims a thread if claimed by yourself.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if not str(ctx.author.id) == thread_data["main_claimer"]:
            embed = discord.Embed(
                title="Thread not claimed by you.",
                description=f"You have not claimed this thread. Ask the claimer <@{thread_data['main_claimer']}> to unclaim it.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        original_name = thread_data.get("original_channel_name")
        if original_name:
            try:
                await ctx.thread.channel.edit(
                    name=original_name[:100],
                    reason=f"Ticket unclaimed by {ctx.author}",
                )
            except discord.HTTPException:
                logger.warning("Failed to restore unclaimed thread name %s.", ctx.thread.channel.id, exc_info=True)
        await self.db.delete_one({"channel_id": str(ctx.thread.channel.id)})
        embed = discord.Embed(
            title="Thread unclaimed",
            description="You successfully unclaimed this thread.",
            color=ctx.bot.main_color,
        )
        return await ctx.send(embed=embed)

    @commands.command(name="faddclaim")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    async def faddclaim(self, ctx: commands.Context, *, member: discord.Member):
        """
        Forces addclaim of a thread via the MODERATOR permission.

        Allowes moderators to force addclaim a thread. It does not check anything regarding thread claimer.
        You can overwrite the permissions via the ``perms override`` commmand if needed.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if str(member.id) in thread_data["claimers"]:
            embed = discord.Embed(
                title="Member already added",
                description=f"The member {member.mention} is already added to the claimers.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        thread_data["claimers"].append(str(member.id))
        await self.db.update_one({"channel_id": str(ctx.thread.channel.id)}, {"$set": thread_data})
        embed = discord.Embed(
            title="Member added",
            description=f"You successfully added {member.mention} to the claimers.",
            color=ctx.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="fremoveclaim")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    async def fremoveclaim(self, ctx: commands.Context, *, member: discord.Member):
        """
        Forces removeclaim of a thread via the MODERATOR permission.

        Allowes moderators to force removeclaim a thread. It does not check anything regarding thread claimer.
        You can overwrite the permissions via the ``perms override`` commmand if needed.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if not str(member.id) in thread_data["claimers"]:
            embed = discord.Embed(
                title="Member not added",
                description=f"The member {member.mention} is not added to the claimers.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        if str(member.id) == str(ctx.author.id):
            embed = discord.Embed(
                title="You cannot remove yourself",
                description=f"You cannot remove yourself from the claimers. Unclaim the thread instead.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        thread_data["claimers"].remove(str(member.id))
        await self.db.update_one({"channel_id": str(ctx.thread.channel.id)}, {"$set": thread_data})
        embed = discord.Embed(
            title="Member removed",
            description=f"You successfully removed {member.mention} from the claimers.",
            color=ctx.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="funclaim")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    async def funclaim(self, ctx: commands.Context):
        """
        Forces unclaim of a thread via the MODERATOR permission.

        Allowes moderators to force unclaim a thread. It does not check anything regarding thread claimer.
        You can overwrite the permissions via the ``perms override`` commmand if needed.
        """
        thread_data = await self.db.find_one({"channel_id": str(ctx.thread.channel.id)})
        if thread_data is None:
            embed = discord.Embed(
                title="Thread not claimed",
                description=f"This thread is not claimed by anyone.",
                color=ctx.bot.error_color,
            )
            return await ctx.send(embed=embed)
        original_name = thread_data.get("original_channel_name")
        if original_name:
            try:
                await ctx.thread.channel.edit(
                    name=original_name[:100],
                    reason=f"Ticket force-unclaimed by {ctx.author}",
                )
            except discord.HTTPException:
                logger.warning("Failed to restore force-unclaimed thread name %s.", ctx.thread.channel.id, exc_info=True)
        await self.db.delete_one({"channel_id": str(ctx.thread.channel.id)})
        embed = discord.Embed(
            title="Thread unclaimed",
            description="You successfully forced unclaim of this thread.",
            color=ctx.bot.main_color,
        )
        return await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        """Ping the main claimer when the ticket creator sends another message."""
        if from_mod or thread.channel is None:
            return

        thread_data = await self.db.find_one({"channel_id": str(thread.channel.id)})
        if not thread_data:
            return

        claimer_id = thread_data.get("main_claimer")
        if not claimer_id:
            return

        try:
            claimer_id = int(claimer_id)
        except (TypeError, ValueError):
            return

        claimer = self.bot.modmail_guild.get_member(claimer_id)
        if claimer is None:
            return

        embed = discord.Embed(
            title="New reply",
            description=f"{claimer.mention}, the ticket creator has replied.",
            color=self.bot.main_color,
        )
        try:
            await thread.channel.send(
                content=claimer.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[claimer],
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.HTTPException:
            logger.warning("Failed to ping claimer in thread %s.", thread.channel.id, exc_info=True)

    @commands.group(name="claimconfig", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def claimconfig(self, ctx: commands.Context):
        """
        Plugin configuration management.
        """
        embed = discord.Embed(
            title="Claim configuration",
            color=ctx.bot.main_color,
        )
        for key, value in self.config.items():
            field_value = f"`{value}`"
            description = self.config.describe(key)
            if description:
                field_value += f"\n{description}"
            embed.add_field(name=key, value=field_value, inline=False)
        await ctx.send(embed=embed)

    @claimconfig.command(name="require_claim")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def claimconfig_require_claim(self, ctx: commands.Context, value: bool):
        """
        Sets whether a thread must be claimed before someone can reply to it.
        """
        await self.config.set("require_claim", value)
        embed = discord.Embed(
            title="Config updated",
            description=f"`require_claim` is now set to `{value}`.",
            color=ctx.bot.main_color,
        )
        await ctx.send(embed=embed)


async def setup(bot: ModmailBot):
    await bot.add_cog(Claim(bot))
