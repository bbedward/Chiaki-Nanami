import asyncio
import asyncqlio
import contextlib
import datetime
import discord
import functools
import heapq
import itertools

from collections import Counter, deque, namedtuple
from discord.ext import commands
from operator import attrgetter, contains, itemgetter

from .tables.base import TableBase
from .utils import dbtypes, errors, formats, time
from .utils.context_managers import redirect_exception, temp_attr
from .utils.converter import in_, union
from .utils.jsonf import JSONFile
from .utils.misc import emoji_url, ordinal
from .utils.paginator import ListPaginator, EmbedFieldPages

from core.cog import Cog


class Warn(TableBase, table_name='warn_entries'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)

    guild_id = asyncqlio.Column(asyncqlio.BigInt)
    user_id = asyncqlio.Column(asyncqlio.BigInt)
    mod_id = asyncqlio.Column(asyncqlio.BigInt)
    reason = asyncqlio.Column(asyncqlio.String(2000))
    warned_at = asyncqlio.Column(asyncqlio.Timestamp)


# XXX: Should I have the timeout and punishments as one table?
class WarnTimeout(TableBase, table_name='warn_timeouts'):
    guild_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    timeout = asyncqlio.Column(dbtypes.Interval)

class WarnPunishment(TableBase, table_name='warn_punishments'):
    guild_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    warns = asyncqlio.Column(asyncqlio.SmallInt, primary_key=True)
    type = asyncqlio.Column(asyncqlio.String(32))
    duration = asyncqlio.Column(asyncqlio.Integer, default=0)

class MuteRole(TableBase, table_name='muted_roles'):
    guild_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    role_id = asyncqlio.Column(asyncqlio.BigInt)


class _ConflictColumns(namedtuple('_ConflictColumns', 'columns')):
    """Hack to support multiple columns for on_conflict"""
    __slots__ = ()

    @property
    def quoted_name(self):
        return ', '.join(c.quoted_name for c in self.columns)

WarnPunishmentCC = _ConflictColumns((WarnPunishment.guild_id, WarnPunishment.warns))
del _ConflictColumns


# Dummy punishment class for default warn punishment
_DummyPunishment = namedtuple('_DummyPunishment', 'warns type duration')
_default_punishment = _DummyPunishment(warns=3, type='mute', duration=60 * 10)
del _DummyPunishment


class MemberID(union):
    def __init__(self):
        super().__init__(discord.Member, int)

    async def convert(self, ctx, arg):
        member = await super().convert(ctx, arg)
        if isinstance(member, int):
            obj = discord.Object(id=member)
            obj.__str__ = attrgetter('id')
            obj.guild = ctx.guild
            return obj
        return member


class BannedMember(commands.Converter):
    async def convert(self, ctx, arg):
        ban_list = await ctx.guild.bans()
        try:
            member_id = int(arg, base=10)
        except ValueError:
            thing = discord.utils.find(lambda e: str(e.user) == arg, ban_list)
        else:
            thing = discord.utils.find(lambda e: e.user.id == member_id, ban_list)

        if thing is None:
            raise commands.BadArgument(f"{arg} wasn't previously-banned in this server...")
        return thing


_warn_punishments = ['mute', 'kick', 'softban', 'tempban', 'ban',]
_is_valid_punishment = frozenset(_warn_punishments).__contains__


# TODO:
# - implement anti-raid protocol
# - implement antispam
# - implement mention-spam
class Moderator(Cog):
    def __init__(self, bot):
        self.bot = bot

        self.slowmodes = JSONFile('slowmodes.json')
        self.slowmode_bucket = {}

    async def call_mod_log_invoke(self, invoke, ctx):
        mod_log = ctx.bot.get_cog('ModLog')
        if mod_log:
            await getattr(mod_log, f'mod_{invoke}')(ctx)

    __before_invoke = functools.partialmethod(call_mod_log_invoke, 'before_invoke')
    __after_invoke = functools.partialmethod(call_mod_log_invoke, 'after_invoke')

    # ---------------- Slowmode ------------------

    @staticmethod
    def _is_slowmode_immune(member):
        return member.guild_permissions.manage_guild

    async def check_slowmode(self, message):
        if message.guild is None:
            return

        guild_id = message.guild.id
        if guild_id not in self.slowmodes:
            return

        slowmodes = self.slowmodes[guild_id]

        author = message.author
        is_immune = self._is_slowmode_immune(author)

        for thing in (message.channel, author):
            key = str(thing.id)
            if key not in slowmodes:
                continue

            config = slowmodes[key]
            if not config['no_immune'] and is_immune:
                continue

            bucket = self.slowmode_bucket.setdefault(thing.id, {})
            time = bucket.get(author.id)
            if time is None or (message.created_at - time).total_seconds() >= config['duration']:
                bucket[author.id] = message.created_at
            else:
                await message.delete()
                break

    @commands.group(invoke_without_command=True, usage=['15', '99999 @Mee6#4876'])
    @commands.has_permissions(manage_messages=True)
    async def slowmode(self, ctx, duration: time.Delta, *, member: discord.Member=None):
        """Puts a thing in slowmode.

        An optional member argument can be provided. If it's
        given, it puts only that user in slowmode for the entire
        server. Otherwise it puts the current channel in slowmode.

        Those with Manage Server permissions will not be
        affected. If you want to put them in slowmode too,
        use `{prefix}slowmode noimmune`.
        """
        pronoun = 'They'
        if member is None:
            member = ctx.channel
            pronoun = 'Everyone'
        elif self._is_slowmode_immune(member):
            message = (f"{member} is immune from slowmode due to having the "
                       f"Manage Server permission. Consider using `{ctx.prefix}slowmode "
                        "no-immune` or giving them a harsher punishment.")
            return await ctx.send(message)

        config = self.slowmodes.get(ctx.guild.id, {})
        slowmode = config.setdefault(str(member.id), {'no_immune': False})
        if slowmode['no_immune']:
            return await ctx.send(f'{member.mention} is already in **no-immune** slowmode. '
                                   'You need to turn it off first.')

        slowmode['duration'] = duration.duration
        await self.slowmodes.put(ctx.guild.id, config)

        await ctx.send(f'{member.mention} is now in slowmode! '
                       f'{pronoun} must wait {duration} '
                        'between each message they send.')

    @slowmode.command(name='noimmune', aliases=['n-i'], usage=['10', '1000000000 @b1nzy#1337'])
    @commands.has_permissions(manage_messages=True)
    async def slowmode_no_immune(self, ctx, duration: time.Delta, *, member: discord.Member=None):
        """Puts the channel or member in "no-immune" slowmode.

        Unlike `{prefix}slowmode`, no one is immune to this slowmode,
        even those with Manage Server permissions, which means everyone's messages
        will be deleted if they are within the duration given.
        """
        if member is None:
            member, pronoun = ctx.channel, 'They'
        else:
            pronoun = 'Everyone'

        config = self.slowmodes.get(ctx.guild.id, {})
        slowmode = config.setdefault(str(member.id), {'no_immune': True})
        slowmode['duration'] = duration.duration
        await self.slowmodes.put(ctx.guild, config)

        await ctx.send(f'{member.mention} is now in **no-immune** slowmode! '
                       f'{pronoun} must wait {duration} '
                       'after each message they send.')

    @slowmode.command(name='off', usage=['', '277045400375001091'])
    async def slowmode_off(self, ctx, *, member: discord.Member=None):
        """Turns off slowmode for either a member or channel."""
        member = member or ctx.channel
        config = self.slowmodes.get(ctx.guild.id, {})
        try:
            del config[str(member.id)]
        except KeyError:
            return await ctx.send(f'{member.mention} was never in slowmode... \N{NEUTRAL FACE}')
        else:
            await self.slowmodes.put(ctx.guild.id, config)
            self.slowmode_bucket.pop(member.id, None)
            await ctx.send(f'{member.mention} is no longer in slowmode... '
                           '\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')

    @commands.command(usage=['', '277045400375001091'])
    @commands.has_permissions(manage_messages=True)
    async def slowoff(self, ctx, *, member: discord.Member=None):
        """Alias for `{prefix}slowmode off`"""
        await ctx.invoke(self.slowmode_off, member=member)

    # ----------------------- End slowmode ---------------------

    @commands.command(aliases=['newmembers', 'joined'])
    @commands.guild_only()
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have joined.

        The minimum is 3 members. If no number is given I'll show the last 5 members.
        """
        human_delta = time.human_timedelta
        count = max(count, 3)
        members = heapq.nlargest(count, ctx.guild.members, key=attrgetter('joined_at'))

        names = map(str, members)
        values = (
            (f'**Joined:** {human_delta(member.joined_at)}\n'
             f'**Created:** {human_delta(member.created_at)}\n{"-" * 40}')
            for member in members
        )
        entries = zip(names, values)

        title = f'The {formats.pluralize(**{"newest members": len(members)})}'
        pages = EmbedFieldPages(ctx, entries, lines_per_page=5, colour=0x00FF00, title=title)
        await pages.interact()

    @commands.command(aliases=['clr'], usage=['', '50', '@Corrupt X#6821'])
    @commands.has_permissions(manage_messages=True)
    async def clear(self, ctx, num_or_user: union(int, discord.Member)=None):
        """Clears some messages in a channels

        The argument can either be a user or a number.
        If it's a number it deletes *up to* that many messages.
        If it's a user, it deletes any message by that user up to the last 100 messages.
        If no argument was specified, it deletes my messages.
        """

        if isinstance(num_or_user, int):
            if num_or_user < 1:
                raise errors.InvalidUserArgument(f"How can I delete {number} messages...?")
            deleted = await ctx.channel.purge(limit=min(num_or_user, 1000) + 1)
        elif isinstance(num_or_user, discord.Member):
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == num_or_user.id)
        else:
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == ctx.bot.user.id)

        deleted_count = len(deleted) - 1
        is_plural = 's'*(deleted_count != 1)
        await ctx.send(f"Deleted {deleted_count} message{is_plural} successfully!", delete_after=1.5)

    @commands.command(aliases=['clean'], usage=['', '10'])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup(self, ctx, limit=100):
        """Cleans up my messages from the channel.

        If I have the Manage Messages and Read Message History perms, I can also
        try to delete messages that look like they invoked my commands.

        When I'm done cleaning up. I will show the stats of whose messages got deleted
        and how many. This should give you an idea as to who are spamming me.

        You can also use this if `{prefix}clear` fails.
        """

        prefixes = tuple(ctx.bot.get_guild_prefixes(ctx.guild))
        bot_id = ctx.bot.user.id

        bot_perms = ctx.channel.permissions_for(ctx.me)
        purge = functools.partial(ctx.channel.purge, limit=limit, before=ctx.message)
        can_bulk_delete = bot_perms.manage_messages and bot_perms.read_message_history

        if can_bulk_delete:
            def is_possible_command_invoke(m):
                if m.author.id == bot_id:
                    return True
                return m.content.startswith(prefixes) and not m.content[1:2].isspace()
            deleted = await purge(check=is_possible_command_invoke)
        else:
            # We can only delete the bot's messages, because trying to delete
            # other users' messages without Manage Messages will raise an error.
            # Also we can't use bulk-deleting for the same reason.
            deleted = await purge(check=lambda m: m.author.id == bot_id, bulk=False)

        spammers = Counter(str(m.author) for m in deleted)
        total_deleted = sum(spammers.values())
        second_part = 's was' if total_deleted == 1 else ' were'
        title = f'{total_deleted} messages{second_part} removed.'
        joined = '\n'.join(itertools.starmap('**{0}**: {1}'.format, spammers.most_common()))
        spammer_stats = joined or discord.Embed.Empty

        embed = (discord.Embed(colour=0x00FF00, description=spammer_stats, timestamp=ctx.message.created_at)
                .set_author(name=title)
                )
        await ctx.send(embed=embed, delete_after=20)
        await asyncio.sleep(20)
        with contextlib.suppress(discord.HTTPException):
            await ctx.message.delete()

    @clear.error
    @cleanup.error
    async def clear_error(self, ctx, error):
        # We need to use the __cause__ because any non-CommandErrors will be
        # wrapped in CommandInvokeError
        cause = error.__cause__
        if isinstance(cause, discord.Forbidden):
            await ctx.send("I need the Manage Messages perm to clear messages.")
        elif isinstance(cause, discord.HTTPException):
            await ctx.send("Couldn't delete the messages for some reason... Here's the error:\n"
                          f"```py\n{type(cause).__name__}: {cause}```")

    async def _get_warn_timeout(self, session, guild_id):
        query = session.select(WarnTimeout).where(WarnTimeout.guild_id == guild_id)
        timeout = await query.first()
        return timeout.timeout if timeout else datetime.timedelta(minutes=15)

    @commands.command(usage=['@XenaWolf#8379 NSFW'])
    @commands.has_permissions(manage_messages=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        """Warns a user (obviously)"""
        self._check_user(ctx, member)
        author, current_time, guild_id = ctx.author, ctx.message.created_at, ctx.guild.id
        timeout = await self._get_warn_timeout(ctx.session, guild_id)
        query = (ctx.session.select.from_(Warn)
                                   .where((Warn.guild_id == guild_id)
                                          & (Warn.user_id == member.id)
                                          & (Warn.warned_at > current_time - timeout)))
        warn_queue = [r async for r in await query.all()]

        try:
            last_warn = warn_queue[-1]
        except IndexError:
            pass
        else:
            retry_after = (current_time - last_warn.warned_at).total_seconds()
            if retry_after <= 60:
                # Must throw an error because return await triggers on_command_completion
                # Which would end up logging a case even though it doesn't work.
                raise RuntimeError(f"{member} has been warned already, try again in "
                                   f"{60 - retry_after :.2f} seconds...")

        entry = Warn(
            guild_id=guild_id,
            user_id=member.id,
            mod_id=author.id,
            reason=reason,
            warned_at=current_time,
        )

        await ctx.session.add(entry)
        current_warn_number = len(warn_queue) + 1
        query = (ctx.session.select(WarnPunishment)
                            .where((WarnPunishment.guild_id == guild_id)
                                   & (WarnPunishment.warns == current_warn_number)))

        punishment = await query.first()
        if punishment is None:
            if current_warn_number == 3:
                punishment = _default_punishment
            else:
                return await ctx.send(f"\N{WARNING SIGN} Warned {member.mention} successfully!")

        # Auto-punish the user
        args = member,
        duration = punishment.duration
        if duration > 0:
            args += duration,
            punished_for = f' for {time.duration_units(duration)}'
        else:
            punished_for = f''

        punish = punishment.type
        punishment_command = getattr(self, punish)
        punishment_reason = f'{reason}\n({ordinal(current_warn_number)} warning)'
        # Patch out the context's send method because we don't want it to be
        # sending the command's message.
        # XXX: Should I suppress the error?
        with temp_attr(ctx, 'send', lambda *a, **kw: asyncio.sleep(0)):
            await ctx.invoke(punishment_command, *args, reason=punishment_reason)

        message = (f"{member.mention} has {current_warn_number} warnings! "
                   f"**It's punishment time!** Today I'll {punish} you{punished_for}! "
                    "\N{SMILING FACE WITH HORNS}")
        await ctx.send(message)

        # Dynamically patch the attributes because case logging requires them.
        # If they weren't patched in, it would treat is as if it was a warn action.
        ctx.auto_punished = True
        ctx.command = punishment_command
        ctx.args[2:] = args
        ctx.kwargs['reason'] = punishment_reason

    @warn.error
    async def warn_error(self, ctx, error):
        original = getattr(error, 'original', None)
        if isinstance(original, RuntimeError):
            await ctx.send(original)

    # XXX: Should this be a group?

    @commands.command(name='clearwarns', usage='MIkusaba')
    @commands.has_permissions(manage_messages=True)
    async def clear_warns(self, ctx, member: discord.Member):
        """Clears a member's warns."""
        await ctx.session.delete.table(Warn).where((Warn.guild_id == ctx.guild.id)
                                                   & (Warn.user_id == member.id))
        await ctx.send(f"{member}'s warns have been reset!")

    @commands.command(name='warnpunish', usage=['4 softban', '5 ban'])
    @commands.has_permissions(manage_messages=True, manage_guild=True)
    async def warn_punish(self, ctx, num: int, punishment, duration: time.Delta = 0):
        """Sets the punishment a user receives upon exceeding a given warn limit.

        Valid punishments are:
        `mute` (requires a duration argument)
        `kick`
        `softban`
        `tempban` (requires a duration argument)
        `ban`
        """
        lowered = punishment.lower()
        if not _is_valid_punishment(lowered):
            message = (f'{lowered} is not a valid punishment.\n'
                       f'Valid punishments: {", ".join(_warn_punishments)}')
            return await ctx.send(message)

        if lowered in {'tempban', 'mute'}:
            if not duration:
                return await ctx.send(f'A duration is required for {lowered}...')
            true_duration = duration.duration
        else:
            true_duration = 0

        extra = f'for {duration}' if duration else ''

        row = WarnPunishment(
            guild_id=ctx.guild.id,
            warns=num,
            type=lowered,
            duration=true_duration,
        )

        await (ctx.session.insert.add_row(row)
                          .on_conflict(WarnPunishmentCC)
                          .update(WarnPunishment.type, WarnPunishment.duration))

        await ctx.send(f'\N{OK HAND SIGN} if a user has been warned {num} times, '
                       f'I will **{lowered}** them {extra}.')

    @commands.command(name='warnpunishments', aliases=['warnpl'])
    async def warn_punishments(self, ctx):
        """Shows this list of warn punishments"""
        query = ctx.session.select(WarnPunishment).where((WarnPunishment.guild_id == ctx.guild.id))
        punishments = [(p.warns, p.type.title(), p.duration) async for p in await query.all()]
        if not punishments:
            punishments += (_default_punishment,)
        punishments.sort()

        entries = (f'{warns} strikes => **{type}** {f"for {time.duration_units(duration)}" if duration else ""}'
                   for warns, type, duration in punishments)
        pages = ListPaginator(ctx, entries, title=f'Punishments for {ctx.guild}')
        await pages.interact()

    @commands.command(name='warntimeout', usage=['10', '15m', '1h20m10s'])
    @commands.has_permissions(manage_messages=True, manage_guild=True)
    async def warn_timeout(self, ctx, duration: time.Delta):
        """Sets the maximum time between the oldest warn and the most recent warn.
        If a user hits a warn limit within this timeframe, they will be punished.
        """
        timeout = datetime.timedelta(seconds=duration.duration)
        row = WarnTimeout(guild_id=ctx.guild.id, timeout=timeout)
        await (ctx.session.insert.add_row(row).on_conflict(WarnTimeout.guild_id)
                          .update(WarnTimeout.timeout))

        await ctx.send(f'Alright, if a user was warned within **{duration}** '
                        'after their oldest warn, bad things will happen.')

    @staticmethod
    def _check_user(ctx, member):
        if ctx.author.id == member.id:
            raise errors.InvalidUserArgument("Please don't hurt yourself. :(")
        if member.id == ctx.bot.user.id:
            raise errors.InvalidUserArgument("Hey, what did I do??")

    async def _get_muted_role(self, guild):
        async with self.bot.db.get_session() as session:
            row = await session.select.from_(MuteRole).where(MuteRole.guild_id == guild.id).first()
        if row is None:
            return None

        return discord.utils.get(guild.roles, id=row.role_id)

    async def _update_muted_role(self, guild, new_role):
        await self._regen_muted_role_perms(new_role, *guild.channels)
        async with self.bot.db.get_session() as session:
            await (session.insert.add_row(MuteRole(guild_id=guild.id, role_id=new_role.id))
                                 .on_conflict(MuteRole.guild_id)
                                 .update(MuteRole.role_id))

    async def _create_muted_role(self, guild):
        role = await guild.create_role(name='Chiaki-Muted', colour=discord.Colour.red())
        await self._update_muted_role(guild, role)
        return role

    async def _setdefault_muted_role(self, server):
        # Role could've been deleted, which means it will be None.
        # So we have to account for that.
        return await self._get_muted_role(server) or await self._create_muted_role(server)

    @staticmethod
    async def _regen_muted_role_perms(role, *channels):
        muted_permissions = dict.fromkeys(['send_messages', 'manage_messages', 'add_reactions',
                                           'speak', 'connect', 'use_voice_activation'], False)
        for channel in channels:
            await channel.set_permissions(role, **muted_permissions)

    async def _do_mute(self, member, when):
        mute_role = await self._setdefault_muted_role(member.guild)
        if mute_role in member.roles:
            raise errors.InvalidUserArgument(f'{member.mention} is already been muted... ;-;')

        await member.add_roles(mute_role)
        args = (member.guild.id, member.id, mute_role.id)
        await self.bot.db_scheduler.add_abs(when, 'mute_complete', args)

    @commands.command(usage=['192060404501839872 stfu about your gf'])
    @commands.has_permissions(manage_messages=True)
    async def mute(self, ctx, member: discord.Member, duration: time.Delta, *, reason: str=None):
        """Mutes a user (obviously)

        This command might take a while when this is used for the 
        first time, as, I have to create a role, and update the 
        channel permissions accordingly. 

        If you want to speed up this process, create a muted
        role yourself and use `{prefix}setmuterole`. However,
        this only happens once.
        (not so obviously)
        """
        self._check_user(ctx, member)
        when = ctx.message.created_at + duration.delta
        await self._do_mute(member, when)
        await ctx.send(f"Done. {member.mention} will now be muted for "
                       f"{duration}... \N{ZIPPER-MOUTH FACE}")

    @commands.command(usage=['80528701850124288', '@R. Danny#6348'])
    async def mutetime(self, ctx, member: discord.Member=None):
        """Shows the time left for a member's mute. Defaults to yourself."""
        if member is None:
            member = ctx.author

        # early out for the case of premature role removal,
        # either by ->unmute or manually removing the role
        role = await self._get_muted_role(ctx.guild)
        if role not in member.roles:
            return await ctx.send(f'{member} is not muted...')

        # This fourth condition is in case we have this scenario:
        # - Member was muted
        # - Mute role was changed while the user was muted
        # - Member was muted again with the new role.
        query = """SELECT expires
                   FROM schedule
                   WHERE event = 'mute_complete'
                   AND args_kwargs #>> '{args,0}' = $1
                   AND args_kwargs #>> '{args,1}' = $2
                   AND args_kwargs #>> '{args,2}' = $3
                   LIMIT 1;
                """

        # We have to go to the lowest level possible, because simply using
        # ctx.session.cursor WILL NOT work, as it uses str.format to format
        # the parameters, which will throw a KeyError due to the {} in the
        # JSON operators.
        session = ctx.session.transaction.acquired_connection
        entry = await session.fetchrow(query, str(ctx.guild.id), str(member.id), str(role.id))
        if entry is None:
            return await ctx.send(f"{member} has been perm-muted, you must've "
                                  "added the role manually or something...")

        when = entry['expires']
        await ctx.send(f'{member} has {time.human_timedelta(when)} remaining. '
                       f'They will be unmuted on {when: %c}.')

    async def _remove_time_entry(self, guild, member, session, *, event='mute_complete'):
        query = """SELECT *
                   FROM schedule
                   WHERE event = $3
                   AND args_kwargs #>> '{args,0}' = $1
                   AND args_kwargs #>> '{args,1}' = $2
                   ORDER BY expires
                   LIMIT 1;
                """
        # We have to go to the lowest level possible, because simply using
        # session.cursor WILL NOT work, as it uses str.format to format
        # the parameters, which will throw a KeyError due to the {} in the
        # JSON operators.
        session = session.transaction.acquired_connection
        entry = await session.fetchrow(query, str(guild.id), str(member.id), event)
        if entry is None:
            return None

        await self.bot.db_scheduler.remove(discord.Object(id=entry['id']))
        return entry

    @commands.command(usage=['@rjt#2336 sorry bb'])
    @commands.has_permissions(manage_messages=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str=None):
        """Unmutes a user (obviously)"""
        role = await self._get_muted_role(member.guild)
        if role not in member.roles:
            return await ctx.send(f"{member} hasn't been muted!")

        await member.remove_roles(role)
        await self._remove_time_entry(member.guild, member, ctx.session)
        await ctx.send(f'{member.mention} can now speak again... '
                        '\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')

    @commands.command(name='regenmutedperms', aliases=['rmp'])
    @commands.is_owner()
    @commands.guild_only()
    async def regen_muted_perms(self, ctx):
        """Creates a muted role (if one wasn't made already) and sets the
        permissions for that role.

        This is mainly a debug command. Which is why it's owner-only. A muted
        role is automatically created when you when first mute a user.
        """
        await self._setdefault_muted_role(ctx.guild)
        await ctx.send('\N{THUMBS UP SIGN}')

    @commands.command(name='setmuterole', aliases=['smur'], usage=['My Cooler Mute Role'])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def set_muted_role(self, ctx, *, role: discord.Role):
        """Sets the muted role for the server.

        Ideally you shouldn't have to do this, as I already create a muted role
        when I attempt to mute someone. This is just in case you already have a
        muted role and would like to use that one instead.
        """
        await self._update_muted_role(ctx.guild, role)
        await ctx.send(f'Set the muted role to **{role}**!')

    @commands.command(name='muterole', aliases=['mur'])
    async def muted_role(self, ctx):
        """Gets the current muted role."""
        role = await self._get_muted_role(ctx.guild)
        msg = ("There is no muted role, either set one now or let me create one for you."
               if role is None else f"The current muted role is **{role}**")
        await ctx.send(msg)

    @commands.command(usage='@Salt#3514 Inferior bot')
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str=None):
        """Kick a user (obviously)"""

        self._check_user(ctx, member)
        await member.kick(reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

    @commands.command(aliases=['sb'], usage='259209114268336129 Enough of your raid fetish.')
    @commands.has_permissions(kick_members=True, manage_messages=True)
    async def softban(self, ctx, member: discord.Member, *, reason: str=None):
        """Softbans a user (obviously)"""

        self._check_user(ctx, member)
        await member.ban(reason=reason)
        await member.unban(reason=f'softban (original reason: {reason})')
        await ctx.send("Done. At least he'll be ok...")

    @commands.command(aliases=['tb'], usage='Kwoth#2560 Your bot sucks lol')
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, duration: time.Delta, *, reason: str=None):
        """Temporarily bans a user (obviously)"""

        self._check_user(ctx, member)
        await ctx.guild.ban(member, reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

        await ctx.bot.db_scheduler.add(duration.delta, 'tempban_complete', (ctx.guild.id, member.id))

    @commands.command(usage='@Nadeko#6685 Stealing my flowers.')
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: MemberID, *, reason: str=None):
        """Bans a user (obviously)

        You can also use this to ban someone even if they're not in the server,
        just use the ID. (not so obviously)
        """
        with contextlib.suppress(AttributeError):
            self._check_user(ctx, member)

        await ctx.guild.ban(member, reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

    @commands.command(unban='@Nadeko#6685 oops')
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user: BannedMember, *, reason: str=None):
        """Unbans the user (obviously)"""

        await ctx.guild.unban(user.user)
        await self._remove_time_entry(ctx.guild, user, ctx.session, event='tempban_complete')
        await ctx.send(f"Done. What did {user.user} do to get banned in the first place...?")

    @commands.command(usage='"theys f-ing up shit" @user1#0000 105635576866156544 user2#0001 user3')
    @commands.has_permissions(ban_members=True)
    async def massban(self, ctx, reason, *members: MemberID):
        """Bans multiple users from the server (obviously)"""
        for m in members:
            await ctx.guild.ban(m, reason=reason)

        await ctx.send(f"Done. What happened...?")

    mute._required_perms    = 'Manage Roles'
    unmute._required_perms  = 'Manage Roles'
    kick._required_perms    = 'Kick Members'
    for cmd in (softban, tempban, ban, unban):
        cmd._required_perms = 'Ban Members'
    del cmd     # cmd still exists outside the for loop, (which is named as unban...)

    @mute.error
    @unmute.error
    @kick.error
    @softban.error
    @tempban.error
    @ban.error
    @unban.error
    @massban.error
    async def mod_action_error(self, ctx, error):
        # We need to use the __cause__ because any non-CommandErrors will be
        # wrapped in CommandInvokeError
        cause = error.__cause__
        command = ctx.command

        if isinstance(cause, discord.Forbidden):
            await ctx.send(f'I need the {command._required_perms} permissions to {command}, I think... '
                            "Or maybe they're just too powerful for me.")
        elif isinstance(cause, discord.HTTPException):
            await ctx.send(f"Couldn't {command} the member for some reason")

    # --------- Events ---------

    async def on_message(self, message):
        await self.check_slowmode(message)

    async def on_guild_channel_create(self, channel):
        server = channel.guild

        # Don't bother creating a mute role if there isn't one set, because
        # people might just want to create a channel without having to deal
        # with moderation commands. Only when people want to use the actual
        # mute command should we create the muted role if there isn't one.
        role = await self._get_muted_role(server)
        if role is None:
            return

        await self._regen_muted_role_perms(role, channel)

    async def on_member_join(self, member):
        # Prevent mute-evasion
        async with self.bot.db.get_session() as session:
            entry = await self._remove_time_entry(member.guild, member, session)
            if entry:
                # mute them for an extra 60 mins
                await self._do_mute(member, entry['expires'] + datetime.timedelta(seconds=3600))

    async def on_member_update(self, before, after):
        # In the event of a manual unmute, this has to be covered.
        removed_roles = set(before.roles).difference(after.roles)
        if not removed_roles:
            return  # Give an early out to save queries.

        role = await self._get_muted_role(before.guild)
        if role in removed_roles:
            async with self.bot.db.get_session() as session:
                # We need to remove this guy from the scheduler in the event of
                # a manual unmute. Because if the guy was muted again, the old
                # mute would still be in effect. So it would just remove the
                # muted role.
                await self._remove_time_entry(before.guild, before, session)

    # XXX: Should I even bother to remove unbans from the scheduler in the event
    #      of a manual unban?

    # -------- Custom Events (used in schedulers) -----------
    async def _wait_for_cache(self, name, guild_id, member_id):
        mod_log = self.bot.get_cog('ModLog')
        if mod_log:
            await mod_log.wait_for_cache(name, guild_id, member_id)

    async def on_mute_complete(self, timer):
        server_id, member_id, mute_role_id = timer.args
        server = self.bot.get_guild(server_id)
        if server is None:
            # rip
            return

        member = server.get_member(member_id)
        if member is None:
            # rip pt. 2
            return

        role = discord.utils.get(server.roles, id=mute_role_id)
        if role is None:
            # not really rip
            return

        await member.remove_roles(role)

    async def on_tempban_complete(self, timer):
        guild_id, user_id = timer.args
        await self._wait_for_cache('tempban', guild_id, user_id)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # rip
            return

        await guild.unban(discord.Object(id=user_id), reason='unban from tempban')


def setup(bot):
    bot.add_cog(Moderator(bot))
