import discord
import enum
import functools

from collections import namedtuple
from discord.ext import commands

from .database import Database

# -----------SPECIAL ROLES-------------

DEFAULT = 'Bot Admin'
class ChiakiRole(enum.Enum):
    admin = 'admin'
    mod = 'moderator'
    permissions = 'permissions'

    def __str__(self):
        return self.value.title()

server_role_default = dict.fromkeys(map(str, ChiakiRole), DEFAULT)
server_roles = Database("admin/adminsandmods.json", default_factory=server_role_default.copy)

def assign_role(server, key, role):
    server_roles[server][str(key)] = getattr(role, 'id', DEFAULT)

def get_role(server, key):
    return server_roles[server][str(key)]

# -----------PREDICATES AND CHECKS------------
class ChiakiCheck(namedtuple('ChiakiCheck', 'predicate roles perms')):
    def __new__(cls, predicate, *, role=None, perms=None):
        return super().__new__(cls, predicate, role, perms)

    async def __call__(self, ctx):
        return await discord.utils.maybe_coroutine(self.predicate, ctx)

def _format_perms(**perms):
    return [f"{'Not' * (not v)} {k.replace('_', ' ').title()}" for k, v in perms.items()]

async def is_owner_predicate(ctx):
    return await ctx.bot.is_owner(ctx.author)

def is_owner():
    return commands.check(ChiakiCheck(is_owner_predicate, role="Bot Owner"))

def server_owner_predicate(guild, author):
    return author.id == guild.owner.id

def is_server_owner():
    return commands.check(ChiakiCheck(lambda ctx: server_owner_predicate(ctx.guild, ctx.author), role="Server Owner"))

def permissions_predicate(ctx, **perms):
    if is_owner_predicate(ctx):
        return True
    resolved = ctx.channel.permissions_for(ctx.author)
    return all(getattr(resolved, perm, None) == value
               for perm, value in perms.items())

def role_predicate(role_key, ctx):
    if is_owner_predicate(ctx):
        return True
    author, server = ctx.author, ctx.guild
    if not server:
        return False
    role_id = get_role(server, role_key)
    role = discord.utils.get(author.roles, id=role_id)
    role_name = discord.utils.get(author.roles, name=DEFAULT)
    return role is not None or role_name is not None

def role_or_perms_predicate(ctx, role, **perms):
    return role_predicate(ctx, role) or permissions_predicate(ctx, **perms)

def has_role(role):
    return commands.check(ChiakiCheck(functools.partial(role_predicate, role), role=str(role)))

def has_perms(**perms):
    return commands.check(ChiakiCheck(lambda ctx: permissions_predicate(ctx, **perms),
                                      perms=_format_perms(**perms)))

def has_role_or_perms(role, **perms):
    return commands.check(ChiakiCheck(lambda ctx: role_or_perms_predicate(ctx, role, **perms),
                                      role=str(role), perms=_format_perms(**perms)))

is_admin = functools.partial(has_role, ChiakiRole.admin)
is_mod = functools.partial(has_role, ChiakiRole.mod)
admin_or_permissions = functools.partial(has_role_or_perms, ChiakiRole.admin)
mod_or_permissions = functools.partial(has_role_or_perms, ChiakiRole.mod)
