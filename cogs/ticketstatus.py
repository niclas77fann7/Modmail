import re

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


class TicketStatus(commands.Cog):
    """Claiming and status controls for Modmail threads."""

    STATUS_PREFIXES = ("claimed-", "waiting-", "urgent-", "open-")

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _thread_key(ctx):
        return str(ctx.thread.id)

    def _base_channel_name(self, channel):
        name = channel.name
        changed = True
        while changed:
            changed = False
            for prefix in self.STATUS_PREFIXES:
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                    changed = True
                    break
        name = re.sub(r"^-+", "", name)
        return name or str(channel.id)

    async def _rename(self, ctx, status):
        base = self._base_channel_name(ctx.channel)
        if status == "open":
            new_name = base
        else:
            new_name = f"{status}-{base}"
        new_name = new_name[:100]
        if ctx.channel.name != new_name:
            await ctx.channel.edit(name=new_name)

    async def _save(self):
        await self.bot.config.update()

    def _claims(self):
        return self.bot.config["ticket_claims"]

    def _statuses(self):
        return self.bot.config["ticket_statuses"]

    def _claimant(self, ctx):
        claimant_id = self._claims().get(self._thread_key(ctx))
        if claimant_id is None:
            return None
        try:
            claimant_id = int(claimant_id)
        except (TypeError, ValueError):
            return None
        return ctx.guild.get_member(claimant_id) or self.bot.get_user(claimant_id)

    def _claimant_mention(self, ctx):
        claimant = self._claimant(ctx)
        if claimant is not None:
            return claimant.mention
        claimant_id = self._claims().get(self._thread_key(ctx))
        return f"<@{claimant_id}>" if claimant_id else None

    def _remove_subscription(self, thread_key, mention):
        if not mention:
            return
        subscriptions = self.bot.config["subscriptions"].setdefault(thread_key, [])
        while mention in subscriptions:
            subscriptions.remove(mention)
        if not subscriptions:
            self.bot.config["subscriptions"].pop(thread_key, None)

    def _add_subscription(self, thread_key, mention):
        subscriptions = self.bot.config["subscriptions"].setdefault(thread_key, [])
        if mention not in subscriptions:
            subscriptions.append(mention)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def claim(self, ctx):
        """Claim the current Modmail ticket."""
        key = self._thread_key(ctx)
        claims = self._claims()
        statuses = self._statuses()

        existing_mention = self._claimant_mention(ctx)
        existing_id = claims.get(key)
        if existing_id is not None and str(existing_id) == str(ctx.author.id):
            embed = discord.Embed(
                title="Ticket Already Claimed",
                description=f"{ctx.author.mention} already has this ticket claimed.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        if existing_mention:
            self._remove_subscription(key, existing_mention)

        claims[key] = str(ctx.author.id)
        statuses[key] = "claimed"
        self._add_subscription(key, ctx.author.mention)

        await self._rename(ctx, "claimed")
        await self._save()

        embed = discord.Embed(
            title="Ticket Claimed",
            description=f"{ctx.author.mention} has claimed this ticket.",
            color=self.bot.main_color,
        )
        embed.add_field(
            name="Notifications",
            value="You will be pinged when the recipient sends a new message.",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unclaim(self, ctx):
        """Remove the current claimant from a Modmail ticket."""
        key = self._thread_key(ctx)
        claims = self._claims()
        statuses = self._statuses()

        claimant_mention = self._claimant_mention(ctx)
        if key not in claims:
            embed = discord.Embed(
                title="Ticket Not Claimed",
                description="This ticket is not currently claimed.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        self._remove_subscription(key, claimant_mention)
        claims.pop(key, None)
        statuses.pop(key, None)

        await self._rename(ctx, "open")
        await self._save()

        embed = discord.Embed(
            title="Ticket Unclaimed",
            description=f"{ctx.author.mention} unclaimed this ticket.",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    async def _set_status(self, ctx, status, title, description, color):
        key = self._thread_key(ctx)
        self._statuses()[key] = status

        await self._rename(ctx, status)
        await self._save()

        embed = discord.Embed(title=title, description=description, color=color)
        claimant = self._claimant_mention(ctx)
        if claimant:
            embed.add_field(name="Claimed by", value=claimant, inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def waiting(self, ctx):
        """Mark the current ticket as waiting."""
        await self._set_status(
            ctx,
            "waiting",
            "Ticket Waiting",
            f"{ctx.author.mention} marked this ticket as waiting.",
            self.bot.main_color,
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def urgent(self, ctx):
        """Mark the current ticket as urgent."""
        await self._set_status(
            ctx,
            "urgent",
            "Ticket Marked Urgent",
            f"{ctx.author.mention} marked this ticket as urgent.",
            self.bot.error_color,
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def normal(self, ctx):
        """Clear waiting/urgent status and return to claimed or open."""
        key = self._thread_key(ctx)
        status = "claimed" if key in self._claims() else "open"
        if status == "open":
            self._statuses().pop(key, None)
        else:
            self._statuses()[key] = status

        await self._rename(ctx, status)
        await self._save()

        embed = discord.Embed(
            title="Ticket Status Normal",
            description=f"{ctx.author.mention} returned this ticket to normal priority.",
            color=self.bot.main_color,
        )
        claimant = self._claimant_mention(ctx)
        if claimant:
            embed.add_field(name="Claimed by", value=claimant, inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=["tstatus"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def ticketstatus(self, ctx):
        """Show the current ticket status and claimant."""
        key = self._thread_key(ctx)
        claimant = self._claimant_mention(ctx)
        status = self._statuses().get(key)
        if status is None:
            status = "claimed" if claimant else "open"

        embed = discord.Embed(
            title="Ticket Status",
            color=self.bot.error_color if status == "urgent" else self.bot.main_color,
        )
        embed.add_field(name="Status", value=status.title(), inline=True)
        embed.add_field(name="Claimed by", value=claimant or "Nobody", inline=True)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(TicketStatus(bot))
