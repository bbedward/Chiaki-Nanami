import inspect
from discord.ext import commands
from functools import wraps

class PrivateMessagesOnly(commands.CommandError):
    """Exception raised when an operation only works in private message contexts."""

class InvalidUserArgument(commands.UserInputError):
    """Exception raised when the user inputs an invalid argument, even though conversion is successful."""

class ResultsNotFound(commands.UserInputError):
    """Exception raised when a search returns some form of "not found" """

def private_message_only(error_msg="This command can only be used in private messages", is_method=True):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not args[is_method].message.channel.is_private:
                raise PrivateMessagesOnly(error_msg)
            return await func(*args, **kwargs)
        return wrapper
    return decorator