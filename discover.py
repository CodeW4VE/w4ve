"""Finding the answer instead of asking somebody to paste it.

An id is where an installation dies. "Copy your guild id" means: turn on
developer mode, right click the right thing, hope you copied the server and
not the channel, paste eighteen digits with no way to tell whether they are the
right eighteen digits. Every one of those steps can fail silently, and the
error arrives much later as a bot that says nothing in a channel nobody is
watching.

So: a setting can say `discover = "discord.channel"`, and w4ve goes and asks.
With a bot token in hand, Discord will happily list the servers it is in, their
channels and their roles, with names on them. The person picks a name from a
numbered list, and the id never touches a clipboard.

The provider is deliberately dumb: no library, no cache, no state. It makes at
most one HTTP request per question and it says what went wrong in words.

Standard library only, Python 3.9.
"""

import json
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
UA = "w4ve (https://github.com/CodeW4VE/w4ve)"

# What the bot needs to be able to do for a chat bridge to work at all. Written
# out so the invite link is right the first time instead of being fixed later
# by somebody reading permission tables.
BRIDGE_PERMISSIONS = {
    "View Channels": 1 << 10,
    "Send Messages": 1 << 11,
    "Manage Webhooks": 1 << 29,
    "Read Message History": 1 << 16,
    "Attach Files": 1 << 15,
    "Embed Links": 1 << 14,
    "Use External Emojis": 1 << 18,
    "Add Reactions": 1 << 6,
}

# Channel types Discord uses. 0 is a normal text channel, 5 an announcement
# one; the rest (voice, categories, threads) are not places a bridge writes to.
TEXT_CHANNELS = (0, 5)


class DiscoveryError(Exception):
    """Could not go and look, and the message says why in plain words."""


def _get(path, token):
    request = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bot " + token, "User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise DiscoveryError(
                "Discord says that token is not valid. It is the **bot** token "
                "from the Bot tab, not the client secret and not the "
                "application id.")
        if exc.code == 403:
            raise DiscoveryError(
                "the bot is not allowed to see that. Invite it again with the "
                "link `w4ve configure` printed, or give it access to the "
                "channel.")
        if exc.code == 429:
            raise DiscoveryError("Discord is rate limiting us: wait a minute "
                                 "and run it again")
        raise DiscoveryError("Discord answered %s for %s" % (exc.code, path))
    except urllib.error.URLError as exc:
        raise DiscoveryError("could not reach Discord: %s" % exc.reason)


# ------------------------------------------------------------------ providers

def discord_me(token):
    """Who this token is, which is also the cheapest way to validate it."""
    return _get("/users/@me", token)


def discord_guilds(token):
    """[(id, name)] of the Discord servers the bot is in."""
    guilds = _get("/users/@me/guilds", token)
    if not guilds:
        raise DiscoveryError(
            "this bot is not in any Discord server yet. Invite it with the "
            "link above, then run this again.")
    return [(g["id"], g["name"]) for g in guilds]


def discord_channels(token, guild_id):
    """[(id, name)] of the text channels of one guild, categories folded in."""
    channels = _get("/guilds/%s/channels" % guild_id, token)
    names = {c["id"]: c.get("name", "") for c in channels if c.get("type") == 4}
    out = []
    for channel in channels:
        if channel.get("type") not in TEXT_CHANNELS:
            continue
        parent = names.get(channel.get("parent_id"))
        label = "#" + channel.get("name", "")
        if parent:
            label = "%s / %s" % (parent, label)
        out.append((channel["id"], label))
    if not out:
        raise DiscoveryError("that Discord server has no text channels the bot "
                             "can see")
    return sorted(out, key=lambda pair: pair[1].lower())


def discord_roles(token, guild_id):
    """[(id, name)] of the roles of one guild, @everyone left out."""
    roles = _get("/guilds/%s/roles" % guild_id, token)
    out = [(r["id"], "@" + r.get("name", "")) for r in roles
           if r.get("name") != "@everyone"]
    if not out:
        raise DiscoveryError("that Discord server has no roles besides "
                             "@everyone")
    return sorted(out, key=lambda pair: pair[1].lower())


def invite_url(application_id):
    """The link that invites the bot with exactly the permissions it needs."""
    total = 0
    for value in BRIDGE_PERMISSIONS.values():
        total |= value
    return ("https://discord.com/oauth2/authorize?client_id=%s&scope=bot"
            "&permissions=%d" % (application_id, total))


# --------------------------------------------------------------- the registry

def options_for(what, token, context=None):
    """Answer a `discover` string with [(value, label)].

    `context` carries what earlier answers already settled, which is how
    `discord.channel` knows which guild to look in without asking twice.
    """
    context = context or {}
    if not token:
        raise DiscoveryError("there is no Discord token set yet, so there is "
                             "nothing to ask with")
    if what == "discord.guild":
        return discord_guilds(token)
    if what in ("discord.channel", "discord.role"):
        guild = context.get("discord_guild") or context.get("guild_id")
        if not guild:
            raise DiscoveryError("the Discord server has to be chosen first")
        if what == "discord.channel":
            return discord_channels(token, guild)
        return discord_roles(token, guild)
    raise DiscoveryError("w4ve does not know how to look up %r" % what)


def token_setting_for(what):
    """Which setting holds the credential a lookup needs."""
    return "discord_token" if str(what).startswith("discord.") else ""
