from .prefixes import Prefixes
from .permissions import Permissions
from .welcome import WelcomeMessages


class Config(Prefixes, Permissions, WelcomeMessages):
    """Commands related to any sort of configuration for Chiaki, i.e. me."""

def setup(bot):
    bot.add_cog(Config(bot))
