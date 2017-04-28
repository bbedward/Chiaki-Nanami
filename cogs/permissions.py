import contextlib
import discord
import enum

from collections import defaultdict
from discord.ext import commands
from itertools import chain

from .utils import checks, errors
from .utils.converter import BotCogConverter, BotCommandsConverter
from .utils.database import Database
from .utils.misc import str_join, usage

_sign = lambda x: (x > 0) - (x < 0)
# If anyone can make a prettier way of making this,
# Don't hesitate to make a pull request...
_default_perm = {
        "global": None,
        "server": {},
        "channel": {},
        "role": {},
        "user": {},
        "userglobal": {},
        }

_level_explanation = """
A {thing} can be allowed or blocked on one of 6 levels:
    global     = Affects all servers (owner-only)
    server     = Affects this particular server
    channel    = Affects the channels specified, or the current channel if not specified
    role       = Affects the roles specified (at least one must be specified)
    user       = Affects the users specified on this server
    userglobal = Affects the users specified for all servers (owner-only)
"""

def _explain_levels(thing):
    import re
    def wrapper(func):
        func.__doc__ = re.sub("(?<=\n)( *)", '', func.__doc__)
        func.__doc__ += _level_explanation.format(thing=thing)
        return func
    return wrapper

class PermAction(enum.Enum):
    ALLOW = (True, 'allow', 'enabled', ':white_check_mark:' )
    NONE =  (None, 'none', 'reset', ':arrows_counterclockwise:' )
    DENY =  (False, 'deny', 'disabled', ':no_entry_sign:')

    def __init__(self, val, mode, action, emoji):
        self.value_ = val
        self.mode = mode
        self.action = action
        self.emoji = emoji

    def __str__(self):
        return self.mode

    @classmethod
    def convert(cls, arg):
        mode = arg.lower()
        if mode in ('allow', 'unlock', 'enable', ):
            return cls.ALLOW
        elif mode in ('none', 'reset', 'null', ):
            return cls.NONE
        elif mode in ('deny', 'lock', 'disable', ):
            return cls.DENY
        raise commands.BadArgument(f"Don't know what to do with {arg}.")

def _assert_no_args(*args, name=""):
    if args:
        raise commands.TooManyArguments(f"There shouldn't be any arguments for the {name} level, I think")

def _server_swallow(ctx, *args):
    _assert_no_args(*args, name="server")
    return [ctx.message.server]

def _global_swallow(ctx, *args):
    _assert_no_args(*args, name="global")

def _convert_args(converter, default=None):
    def convert(ctx, *args):
        if args:
            return [converter(ctx, arg).convert() for arg in args]
        elif default:
            return [default(ctx)]
        raise commands.MissingRequiredArgument()
    return convert

def _role_getter(d, ctx):
    roles = sorted(getattr(ctx.message.server, "roles", []), reverse=True)
    mapper = {True: 1, False: -1, None: 0}
    score = sum(mapper[d.get(role.id)] for role in roles)
    return [None, True, False][_sign(score)]

def _safe_server_id(ctx):
    return getattr(ctx.message.server, 'id', '')

class PermLevel(enum.Enum):
    GLOBAL = (_global_swallow, lambda d, ctx: d)
    SERVER = (_server_swallow, lambda d, ctx: d.get(_safe_server_id(ctx)))
    CHANNEL = (_convert_args(commands.ChannelConverter, lambda ctx: ctx.message.channel),
               lambda d, ctx: d.get(ctx.message.channel.id))
    ROLE = (_convert_args(commands.RoleConverter), _role_getter)
    USER = (_convert_args(commands.MemberConverter),
            lambda d, ctx: d.setdefault(_safe_server_id(ctx), {}).get(ctx.message.author.id))
    USERGLOBAL = (_convert_args(commands.UserConverter),
                  lambda d, ctx: d.get(ctx.message.author.id))

    def __init__(self, arg_parser, getter):
        self.arg_parser = arg_parser
        self.getter = getter

    def __str__(self):
        return self.name.lower()

    @classmethod
    def convert(cls, level):
        try:
            return cls[level.upper()]
        except KeyError:
            raise commands.BadArgument(f"Unrecognized level: {level}")

def _all_cmd_names(cmd):
    return [cmd.qualified_name.split()[0], *cmd.aliases]

class Permissions:
    __prefix__ = ';_;'

    def __init__(self, bot):
        self.bot = bot
        self.permissions = Database.from_json('permissions2.json', default_factory=_default_perm.copy)
        self.blacklists = Database.from_json('specialuserperms.json', default_factory=list)

    def _restricted_commands(self):
        b = self.bot
        commands = chain.from_iterable(b.cog_command_namespace[k]['commands'] for k in self._restricted_cogs)
        return set(chain.from_iterable(map(_all_cmd_names, commands))) | {'help'}

    @property
    def _restricted_cogs(self):
        return {'Owner', 'Help', 'Permissions'}

    def _assert_is_valid(self, cmds):
        cmd = cmds if isinstance(cmds, str) else cmds[0]
        if cmd in self._restricted_commands():
            raise errors.InvalidUserArgument(f"This command ({cmd}) cannot have its permissions modified")
        elif cmd in self._restricted_cogs:
            raise errors.InvalidUserArgument(f"Module ({cmd}) cannot be disabled")

    def _get_cmddbs(self, cmds):
        self._assert_is_valid(cmds)
        return (self.permissions[name] for name in cmds)

    def _perm_reset(self, ctx, level, cmd, *ids):
        for db in self._get_cmddbs(cmd):
            for id in ids:
                db[str(level)].pop(id, None)

    async def _perm_set(self, ctx, level, mode, cmd, *idables):
        # Couldn't think of a prettier way
        if level == PermLevel.GLOBAL:
            if not checks.is_owner_predicate(ctx):
                raise commands.CheckFailure("GLOBAL level is owner-only")
            for cmddb in self._get_cmddbs(cmd):
                # By default, global must be set to None
                # Otherwise it will terminate the check early
                cmddb[str(level)] = False if mode == PermAction.DENY else None
        else:
            if level == PermLevel.USERGLOBAL and not checks.is_owner_predicate(ctx):
                raise commands.CheckFailure("USERGLOBAL level is owner-only")
            ids = [idable.id for idable in idables]
            if mode == PermAction.NONE:
                self._perm_reset(ctx, level, cmd, *ids)
            elif level == PermLevel.SERVER and mode != PermAction.DENY:
                # Wouldn't make sense to "allow" it
                self._perm_reset(ctx, level, cmd, *ids)
            else:
                for cmddb in self._get_cmddbs(cmd):
                    cmddb[str(level)].update(dict.fromkeys(ids, mode.value_))

        await self.bot.say(f"{mode.emoji} Successfully {mode.action} **\"{', '.join(cmd)}\"**"
                           f" on the **{level}** level, I think.\n"
                           f"Affected {level}s: ```{str_join(', ', idables)}```")

    def _perm_iterator(self, ctx, cmd):
        cmddb = self.permissions[cmd]
        return ((level.getter(cmddb[str(level)], ctx), level) for level in reversed(PermLevel))

    def __check(self, ctx):
        #if checks.is_owner_predicate(ctx):
            #return True
        if ctx.message.author.id in self.blacklists['blacklist']:
            return False

        cmd = ctx.command
        name = cmd.qualified_name.split(' ')[0]
        try:
            self._assert_is_valid(name)
        except errors.InvalidUserArgument:
            return True

        for result, _ in self._perm_iterator(ctx, name):
            if result is not None:
                return result

        cog_name = cmd.cog_name or "No Category"
        for result, _ in self._perm_iterator(ctx, cog_name):
            if result is not None:
                return result
        return True

    def _perms(self, ctx, name):
        perm_mapper = {True: '+', None: ' ', False: '-'}
        return [f"{perm_mapper[res]} {level}" for res, level in self._perm_iterator(ctx, name)]

    def _perm_str(self, ctx, name):
        perms = '\n'.join(self._perms(ctx, name))
        return f'There are the perms for **{name}**```diff\n{perms}```'

    @commands.command(name='permcmd', pass_context=True, no_pm=True, aliases=['pcmd'])
    async def perm_command(self, ctx, cmd_name):
        """Returns the permissions for each level for a given command"""
        cmd = self.bot.get_command(cmd_name)
        if cmd is None:
            raise ResultsNotFound(f"I don't recognise command \"{cmd_name}\"")
        await self.bot.say(self._perm_str(ctx, cmd.qualified_name.split()[0]))

    @commands.command(name='permmod', pass_context=True, no_pm=True, aliases=['permcog', 'pmod'])
    async def perm_module(self, ctx, module: BotCogConverter):
        """Returns the permissions for each level for a given module"""
        await self.bot.say(self._perm_str(ctx, module.name))

    @commands.command(name='psetcommand', pass_context=True, no_pm=True, aliases=['psc'])
    @checks.is_admin()
    @_explain_levels("command")
    @usage("psc channel lock cp #general", "psc server allow salt")
    async def perm_set_command(self, ctx, level: PermLevel.convert, mode: PermAction.convert,
                               cmd: BotCommandsConverter, *idables):
        """Sets a command's permissions. For convenience (and to prevent loopholes),
        this will also lock its aliases as well.
        """
        # There is however, no easy way to lock just a particular subcommand...
        idables = level.arg_parser(ctx, *idables)
        await self._perm_set(ctx, level, mode, cmd.name, *idables)

    @commands.command(name='psetmodule', pass_context=True, no_pm=True, aliases=['psm'])
    @checks.is_admin()
    @_explain_levels("module")
    @usage("psm user lock trivia @SomeGuy @SomeGirl @SomeThing", "psc server allow NSFW")
    async def perm_set_module(self, ctx, level: PermLevel.convert, mode: PermAction.convert,
                           cog: BotCogConverter, *idables):
        """Sets a module's permissions. The module is case insensitive."""
        idables = level.arg_parser(ctx, *idables)
        await self._perm_set(ctx, level, mode, [cmd], *idables)

    @commands.command(no_pm=True, aliases=['bl'])
    @checks.is_owner()
    async def blacklist(self, *, user: discord.User):
        """Blacklists a user from using the bot"""
        self.blacklists['blacklist'].append(user.id)
        await self.bot.say(f'\N{THUMBS UP SIGN} {user} has been blacklisted')

    @commands.command(no_pm=True, aliases=['wl'])
    @checks.is_owner()
    async def whitelist(self, *, user: discord.User):
        """Whitelists a user from the blacklist. This doesn't make them immune to other checks."""
        with contextlib.suppress(ValueError):
            self.blacklists['blacklist'].remove(user.id)
        await self.bot.say(f'\N{THUMBS UP SIGN} {user} has been removed from the blacklist!')

def setup(bot):
    bot.add_cog(Permissions(bot))
