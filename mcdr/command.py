"""The command tree: how a plugin says what `!!stats rank player` means.

A plugin builds a tree of nodes and hands the root to the host. A line of chat
walks it, each node eating the part it understands, until one that has a
callback runs it. It is Brigadier's idea in Python, and it is what DigAll,
StatsHelper and PrimeBackup are all waiting on.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.

Two things here are not decoration and are the reason a naive version would
fail on real plugins:

  * a plugin can define its own node type. StatsHelper has `ScoreboardQuery`
    and `ArgumentEnding`, both subclasses of `ArgumentNode` with their own
    `parse`. So `parse` returning a `ParseResult` is a public contract, not an
    internal detail.
  * errors are values a node handles, not exceptions that escape. A tree can
    attach `on_error` and `on_child_error`, and StatsHelper leans on both to
    answer "unknown argument" with a clickable suggestion instead of a stack
    trace.
"""

import inspect
import re


# ------------------------------------------------------------------- errors

class CommandError(Exception):
    """Anything that stopped a command from running."""

    def __init__(self, message="", parsed_command="", failed_command=""):
        Exception.__init__(self, message)
        self.message = message
        self.parsed_command = parsed_command
        self.failed_command = failed_command
        self._handled = False

    def set_handled(self):
        self._handled = True

    def is_handled(self):
        return self._handled

    def to_mcdr_string(self):
        return self.message or type(self).__name__


class UnknownCommand(CommandError):
    """The line ran out while the tree still expected something."""


class UnknownArgument(CommandError):
    """There is more line left and no node that understands it."""


class UnknownRootArgument(UnknownArgument):
    """The very first word matched nothing."""


class RequirementNotMet(CommandError):
    """A `requires` said no. Usually permissions."""

    def __init__(self, message="", reason=None, **kwargs):
        CommandError.__init__(self, message, **kwargs)
        self.reason = reason

    def has_custom_reason(self):
        return self.reason is not None

    def get_reason(self):
        return self.reason


class IllegalArgument(CommandError):
    """A node understood which argument this is and not its value."""


class IllegalNodeOperation(Exception):
    """The tree itself is built wrong, which is a bug in the plugin."""


class NumberOutOfRange(IllegalArgument):
    pass


class InvalidNumber(IllegalArgument):
    pass


class InvalidInteger(InvalidNumber):
    pass


class InvalidFloat(InvalidNumber):
    pass


class InvalidBoolean(IllegalArgument):
    pass


class InvalidEnumeration(IllegalArgument):
    pass


class EmptyText(IllegalArgument):
    pass


class UnclosedQuotedString(IllegalArgument):
    pass


class IllegalEscapesUsage(IllegalArgument):
    pass


class ParseResult:
    """What a node ate: the value it produced and how much line it used."""

    def __init__(self, value, char_read):
        self.value = value
        self.char_read = char_read

    def __repr__(self):
        return "ParseResult(%r, %d)" % (self.value, self.char_read)


# ------------------------------------------------------------------ context

class CommandContext(dict):
    """The values collected so far, plus who is running the command.

    A dict on purpose: plugins read it as `ctx['player']`, and StatsHelper even
    reads a key with a slash in it (`ctx['cls/alias']`), so nothing clever can
    be layered on top of the key.
    """

    def __init__(self, source, command=""):
        dict.__init__(self)
        self.source = source
        self.command = command
        self.command_read = ""

    def copy(self):
        clone = CommandContext(self.source, self.command)
        clone.update(self)
        clone.command_read = self.command_read
        return clone


# -------------------------------------------------------------------- nodes

def _call_failure(failure, source, context):
    """Ask a requirement why it said no, whatever shape the getter is.

    MCDR types this one as taking nothing, and that is what plugins write:
    `requires(pred, lambda: 'you need admin')`. Calling it with a source the
    way callbacks are called would raise a TypeError from inside the tree,
    turning a polite refusal into a stack trace. Both shapes are accepted.
    """
    if failure is None:
        return None
    try:
        return failure()
    except TypeError:
        return _call(failure, source, context)


def _call(callback, source, context):
    """Call a plugin's callback with what it declared it wants.

    Plugins write both `def f(source)` and `def f(source, context)`, and
    lambdas of either shape. Counting the parameters is how MCDR decides, so
    it is how this decides.
    """
    try:
        signature = inspect.signature(callback)
        wanted = len([p for p in signature.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                      and p.default is p.empty])
    except (TypeError, ValueError):
        wanted = 2
    if wanted <= 1:
        return callback(source)
    return callback(source, context)


class AbstractNode:
    """One step of a command. Everything in the tree derives from this."""

    def __init__(self):
        self.children = []
        self._callback = None
        self._requirements = []     # (predicate, failure message factory)
        self._redirect = None
        self._error_handlers = {}        # error type -> (handler, handled)
        self._child_error_handlers = {}
        self._suggester = None

    # -------------------------------------------------------------- building

    def then(self, node):
        if self._redirect is not None:
            raise IllegalNodeOperation("a redirecting node cannot have children")
        self.children.append(node)
        return self

    def runs(self, callback):
        self._callback = callback
        return self

    def requires(self, predicate, failure_message_getter=None):
        self._requirements.append((predicate, failure_message_getter))
        return self

    def redirects(self, node):
        if self.children:
            raise IllegalNodeOperation("a node with children cannot redirect")
        self._redirect = node
        return self

    def on_error(self, error_type, handler, handled=False):
        self._error_handlers[error_type] = (handler, handled)
        return self

    def on_child_error(self, error_type, handler, handled=False):
        self._child_error_handlers[error_type] = (handler, handled)
        return self

    def suggests(self, suggester):
        self._suggester = suggester
        return self

    def suggestions(self, context=None):
        if self._suggester is None:
            return []
        result = _call_suggester(self._suggester, context)
        return list(result or [])

    # --------------------------------------------------------------- parsing

    def parse(self, text):
        """Eat the front of `text` and say what it produced.

        Subclasses implement this, including subclasses written by plugins.
        """
        raise NotImplementedError

    def _literal_name(self):
        return None

    # --------------------------------------------------------------- running

    def execute(self, source, command):
        """Walk the tree for one command line. Returns True if something ran."""
        context = CommandContext(source, command)
        try:
            return self._walk(source, command, context)
        except CommandError as error:
            if not self._dispatch_error(self, error, source, context, own=True):
                raise
            return True

    def _walk(self, source, remaining, context):
        node = self._redirect or self
        if node is not self:
            return node._walk(source, remaining, context)

        for predicate, failure in self._requirements:
            if not _call(predicate, source, context):
                raise RequirementNotMet("requirement not met",
                                        reason=_call_failure(failure, source,
                                                             context))

        text = remaining.lstrip()
        result = self.parse(text)
        if result is None:
            raise UnknownArgument("cannot parse %r" % text, failed_command=text)

        if getattr(self, "name", None) and result.value is not None:
            context[self.name] = result.value
        context.command_read = (context.command_read + " " +
                                text[:result.char_read]).strip()
        rest = text[result.char_read:]

        if not rest.strip():
            if self._callback is not None:
                _call(self._callback, source, context)
                return True
            raise UnknownCommand("nothing runs at %r" % context.command_read,
                                 parsed_command=context.command_read)

        rest = rest.lstrip()
        for child in self.children:
            if not child._could_match(rest):
                continue
            try:
                return child._walk(source, rest, context.copy())
            except CommandError as error:
                if self._dispatch_error(child, error, source, context,
                                        own=False):
                    return True
                raise
        raise UnknownArgument("no branch for %r" % rest, failed_command=rest,
                              parsed_command=context.command_read)

    def _could_match(self, text):
        """A cheap check so a wrong branch is skipped rather than exploded in."""
        return True

    def _dispatch_error(self, node, error, source, context, own):
        """Find something that wants to handle this, nearest first."""
        handlers = self._error_handlers if own else self._child_error_handlers
        for error_type, (handler, handled) in handlers.items():
            if isinstance(error, error_type):
                _call_error_handler(handler, source, error, context)
                if handled:
                    error.set_handled()
                return True
        return False


def _call_error_handler(handler, source, error, context):
    try:
        signature = inspect.signature(handler)
        wanted = len([p for p in signature.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                      and p.default is p.empty])
    except (TypeError, ValueError):
        wanted = 1
    if wanted <= 1:
        return handler(source)
    if wanted == 2:
        return handler(source, error)
    return handler(source, error, context)


def _call_suggester(suggester, context):
    if not callable(suggester):
        return suggester
    try:
        signature = inspect.signature(suggester)
        wanted = len(signature.parameters)
    except (TypeError, ValueError):
        wanted = 0
    return suggester(context) if wanted else suggester()


class Literal(AbstractNode):
    """A fixed word, or several that mean the same. `!!stats`, `rank`."""

    def __init__(self, literal):
        AbstractNode.__init__(self)
        self.literals = ([literal] if isinstance(literal, str)
                         else list(literal))
        self.name = None

    def _literal_name(self):
        return self.literals[0]

    def _could_match(self, text):
        head = text.split(" ", 1)[0]
        return head in self.literals

    def parse(self, text):
        head = text.split(" ", 1)[0]
        if head in self.literals:
            return ParseResult(None, len(head))
        return None

    def suggestions(self, context=None):
        return AbstractNode.suggestions(self, context) or list(self.literals)

    def __repr__(self):
        return "Literal(%s)" % "|".join(self.literals)


class ArgumentNode(AbstractNode):
    """A named value. Plugins subclass this and write their own `parse`."""

    def __init__(self, name):
        AbstractNode.__init__(self)
        self.name = name

    def get_name(self):
        return self.name

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.name)


class Text(ArgumentNode):
    """One word."""

    def parse(self, text):
        head = text.split(" ", 1)[0]
        if not head:
            raise EmptyText("expected something for %r" % self.name)
        return ParseResult(head, len(head))


class QuotableText(Text):
    """One word, or several inside quotes."""

    def parse(self, text):
        if not text.startswith('"'):
            return Text.parse(self, text)
        collected, escaped, index = [], False, 1
        while index < len(text):
            char = text[index]
            if escaped:
                if char not in ('"', "\\"):
                    raise IllegalEscapesUsage("bad escape \\%s" % char)
                collected.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return ParseResult("".join(collected), index + 1)
            else:
                collected.append(char)
            index += 1
        raise UnclosedQuotedString("no closing quote in %r" % text)


class GreedyText(ArgumentNode):
    """Everything that is left. Always the last node of its branch."""

    def parse(self, text):
        return ParseResult(text, len(text))


class Number(ArgumentNode):
    """A number, with an optional range."""

    def __init__(self, name):
        ArgumentNode.__init__(self, name)
        self._min = None
        self._max = None

    def at_min(self, value):
        self._min = value
        return self

    def at_max(self, value):
        self._max = value
        return self

    def in_range(self, low, high):
        self._min, self._max = low, high
        return self

    def _check(self, value):
        if self._min is not None and value < self._min:
            raise NumberOutOfRange("%s is below %s" % (value, self._min))
        if self._max is not None and value > self._max:
            raise NumberOutOfRange("%s is above %s" % (value, self._max))
        return value

    def parse(self, text):
        head = text.split(" ", 1)[0]
        try:
            value = float(head)
        except ValueError:
            raise InvalidNumber("%r is not a number" % head)
        if value.is_integer():
            value = int(value)
        return ParseResult(self._check(value), len(head))


class Integer(Number):

    def parse(self, text):
        head = text.split(" ", 1)[0]
        try:
            value = int(head)
        except ValueError:
            raise InvalidInteger("%r is not a whole number" % head)
        return ParseResult(self._check(value), len(head))


class Float(Number):

    def parse(self, text):
        head = text.split(" ", 1)[0]
        try:
            value = float(head)
        except ValueError:
            raise InvalidFloat("%r is not a number" % head)
        return ParseResult(self._check(value), len(head))


class Boolean(ArgumentNode):

    def parse(self, text):
        head = text.split(" ", 1)[0]
        if head.lower() in ("true", "yes", "on"):
            return ParseResult(True, len(head))
        if head.lower() in ("false", "no", "off"):
            return ParseResult(False, len(head))
        raise InvalidBoolean("%r is not true or false" % head)


class Enumeration(ArgumentNode):
    """One of a fixed set, usually an Enum class."""

    def __init__(self, name, enum_class=None):
        ArgumentNode.__init__(self, name)
        self.enum_class = enum_class

    def _options(self):
        """name -> value, whether it was given an Enum or a plain mapping.

        A dict is iterable and its keys are strings, so asking for `.name` on
        one blows up in a way that reads like a bug in the plugin. Checking the
        shape first is what keeps the error honest.
        """
        if self.enum_class is None:
            return {}
        if isinstance(self.enum_class, dict):
            return dict(self.enum_class)
        try:
            return {member.name: member for member in self.enum_class}
        except (TypeError, AttributeError):
            return {}

    def parse(self, text):
        head = text.split(" ", 1)[0]
        options = self._options()
        if head not in options:
            raise InvalidEnumeration("%r is not one of %s"
                                     % (head, ", ".join(sorted(options))))
        return ParseResult(options[head], len(head))

    def suggestions(self, context=None):
        return sorted(self._options())


class CountingLiteral(Literal):
    """A literal that also records how many times it appeared.

    PrimeBackup uses it for repeatable flags.
    """

    def __init__(self, literal, name):
        Literal.__init__(self, literal)
        self.name = name

    def parse(self, text):
        result = Literal.parse(self, text)
        if result is None:
            return None
        return ParseResult(1, result.char_read)


class SimpleCommandBuilder:
    """The shorthand: declare paths as strings and let the builder shape them.

    PrimeBackup builds most of its commands this way rather than by nesting
    nodes by hand.
    """

    ARGUMENT = re.compile(r"^<(?P<name>[^>]+)>$")

    def __init__(self):
        self._commands = {}
        self._arguments = {}
        self._post_process = None

    def command(self, path, callback):
        self._commands[path.strip()] = callback
        return self

    def arg(self, name, factory):
        self._arguments[name] = factory
        return self

    def post_process(self, callback):
        self._post_process = callback
        return self

    def _node_for(self, token):
        match = self.ARGUMENT.match(token)
        if not match:
            return Literal(token)
        name = match.group("name")
        factory = self._arguments.get(name)
        if factory is None:
            return Text(name)
        node = factory(name) if callable(factory) else factory
        return node

    def build(self):
        """Turn every declared path into one tree per root word."""
        roots = {}
        for path, callback in self._commands.items():
            tokens = [t for t in path.split(" ") if t]
            if not tokens:
                continue
            head = tokens[0]
            root = roots.get(head)
            if root is None:
                root = self._node_for(head)
                roots[head] = root
            node = root
            for token in tokens[1:]:
                node = self._attach(node, token)
            node.runs(callback)
        result = list(roots.values())
        if self._post_process:
            for root in result:
                self._post_process(root)
        return result

    def _attach(self, parent, token):
        """Reuse a branch that already matches this token, or make one."""
        wanted = self._node_for(token)
        for child in parent.children:
            if (isinstance(child, Literal) and isinstance(wanted, Literal)
                    and child.literals == wanted.literals):
                return child
            if (not isinstance(child, Literal) and not isinstance(wanted, Literal)
                    and child.name == wanted.name):
                return child
        parent.then(wanted)
        return wanted

    def register_to(self, server):
        for root in self.build():
            server.register_command(root)
