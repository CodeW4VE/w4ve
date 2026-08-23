"""Serializable: the typed config object every measured plugin declares.

A plugin writes its configuration as a class with annotations and defaults:

    class Config(Serializable):
        highlight_time: int = 15
        display_xaero_waypoint: bool = True

and then asks the host to load a JSON file into it. What the plugin gets back
has real attributes, and a field missing from the file falls back to the
default rather than exploding, which is what lets a config file survive a
plugin gaining an option.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.

The deliberate difference from MCDR: a value of the wrong type is replaced by
the default and reported, instead of raising. A server that will not boot
because somebody typed `true` where a number goes is worse than a server that
boots and tells them.
"""

import copy
import enum
import json
import re

_MISSING = object()


class Serializable:
    """Base class for typed config and payload objects."""

    def __init__(self, **fields):
        for name, default in self.__field_defaults().items():
            if default is not _MISSING:
                setattr(self, name, copy.deepcopy(default))
        for name, value in fields.items():
            setattr(self, name, value)

    # ---------------------------------------------------------------- fields

    @classmethod
    def __field_types(cls):
        """Annotations of this class and everything it inherits from.

        Walked oldest first so a subclass can override a parent's default,
        which is how plugins specialise a shared config.
        """
        types = {}
        for base in reversed(cls.__mro__):
            types.update(getattr(base, "__annotations__", {}) or {})
        return types

    @classmethod
    def __field_defaults(cls):
        """Field to default, or `_MISSING` for a field declared without one.

        A field with no default is left unset rather than set to None, and
        `serialize` skips it. WhereIs declares `here_use_broadcast: bool` with
        no value and reads it back as `self.serialize().get(key, False)`: with
        a None in there the written config had `"here_use_broadcast": null`,
        which then failed its own type check on the next load.
        """
        defaults = {}
        for name in cls.__field_types():
            value = getattr(cls, name, _MISSING)
            if value is not _MISSING and not callable(value):
                defaults[name] = value
            else:
                defaults[name] = _MISSING
        return defaults

    @classmethod
    def get_field_annotations(cls):
        return dict(cls.__field_types())

    @classmethod
    def get_default(cls):
        """An instance with nothing but the defaults in it.

        Plugins reach for this when they need the untouched config to compare
        against or to write out: Task builds its default file with it, and
        WhereIs asks for the default command prefixes. Missing, both died at
        load with `type object 'Config' has no attribute 'get_default'`.
        """
        return cls()

    # --------------------------------------------------------------- reading

    @classmethod
    def deserialize(cls, data, **kwargs):
        instance, problems = cls.deserialize_reporting(data)
        return instance if not kwargs.get("with_problems") else (instance, problems)

    @classmethod
    def deserialize_reporting(cls, data):
        """Build an instance out of a dict, and say what did not fit.

        Returns (instance, problems). Unknown keys are kept and reported: a
        key nobody reads is usually a typo or a leftover from an older
        version, and deleting somebody's line without telling them is rude.
        """
        return _build(cls, data)

    # --------------------------------------------------------------- writing

    def serialize(self):
        out = {}
        for name in type(self).get_field_annotations():
            value = getattr(self, name, _MISSING)
            if value is _MISSING:
                continue
            out[name] = _serialize_value(value)
        return out

    def to_json_str(self, indent=4):
        return json.dumps(self.serialize(), indent=indent, ensure_ascii=False)

    def copy(self):
        return type(self).deserialize(self.serialize())

    def __eq__(self, other):
        return isinstance(other, type(self)) and self.serialize() == other.serialize()

    def __repr__(self):
        fields = ", ".join("%s=%r" % (k, v) for k, v in self.serialize().items())
        return "%s(%s)" % (type(self).__name__, fields)


def _build(cls, data):
    """Fill a Serializable out of plain data. The generic path, always.

    Kept apart from `Serializable.deserialize` on purpose, because a plugin is
    allowed to override that classmethod and call this back: Task's
    `Task.deserialize` pulls the sub-tasks out of the dict and then asks for
    the rest with `deserialize(data=data, cls=cls)`. If the generic function
    answered by calling the classmethod again, the two would call each other
    until `RecursionError`, which is exactly what happened.
    """
    instance = cls()
    problems = []
    types = cls.get_field_annotations()
    for name, wanted in types.items():
        if name not in (data or {}):
            continue
        value = data[name]
        fixed, why = _coerce(value, wanted, name)
        if why:
            problems.append(why)
            # A partial result is still a result. A collection that lost one
            # bad element keeps the other nineteen; a value that could not be
            # read at all comes back as None and keeps the default.
            if fixed is None:
                continue
        setattr(instance, name, fixed)
    for name in (data or {}):
        if name not in types:
            problems.append("%s is not a setting this plugin reads" % name)
    return instance, problems


def _serialize_value(value):
    """Turn a field into something `json.dumps` will take.

    Recursive on purpose. Real configs nest: QuickBackupM keeps a list of
    `SlotInfo` objects and a dict of them, and a serializer that only looks at
    the top level writes a file that cannot be written, failing with
    "Object of type SlotInfo is not JSON serializable" the first time the
    plugin saves.
    """
    if isinstance(value, Serializable):
        return value.serialize()
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(item) for item in value]
    # An enum is written as its name, not its value. The name is the word a
    # person types in the file, and the value can be anything at all:
    # PrimeBackup's HashMethod holds a dataclass, so writing the value failed
    # with "Object of type _HashMethodItem is not JSON serializable".
    if isinstance(value, enum.Enum):
        return value.name
    # A compiled regex is written as the pattern it was compiled from, which
    # is what a person edits in the file. MinecraftDataAPI declares its
    # `list_output_regex` this way, and without this its very first save
    # failed with "Object of type Pattern is not JSON serializable".
    if isinstance(value, re.Pattern):
        return value.pattern
    return value


def _coerce(value, wanted, name):
    """Make a JSON value fit a declared type, or say why it does not."""
    if wanted is None or wanted is type(None):
        return value, ""
    if isinstance(wanted, type) and issubclass(wanted, Serializable):
        if not isinstance(value, dict):
            return None, "%s should be a block of settings" % name
        return _build(wanted, value)[0], ""
    if wanted is bool:
        if isinstance(value, bool):
            return value, ""
        return None, "%s should be true or false, not %r" % (name, value)
    if wanted is int:
        # A bool is an int in Python and never what the plugin meant here.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "%s should be a whole number, not %r" % (name, value)
        return int(value), ""
    if wanted is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "%s should be a number, not %r" % (name, value)
        return float(value), ""
    if isinstance(wanted, type) and issubclass(wanted, enum.Enum):
        if isinstance(value, wanted):
            return value, ""
        try:
            return wanted[value], ""
        except (KeyError, TypeError):
            return None, "%s should be one of %s, not %r" % (
                name, ", ".join(member.name for member in wanted), value)
    if _is_pattern_type(wanted):
        if isinstance(value, re.Pattern):
            return value, ""
        if not isinstance(value, str):
            return None, "%s should be a regular expression, not %r" % (name, value)
        try:
            return re.compile(value), ""
        except re.error as bad:
            return None, "%s is not a valid regular expression: %s" % (name, bad)
    if wanted is str:
        if not isinstance(value, str):
            return None, "%s should be text, not %r" % (name, value)
        return value, ""
    if wanted in (list, dict, set, tuple):
        if not isinstance(value, wanted):
            return None, "%s should be a %s" % (name, wanted.__name__)
        return value, ""
    # A typing generic like List[str] or Dict[str, SlotInfo].
    outer = getattr(wanted, "__origin__", None)
    if outer in (list, dict, set, tuple):
        if not isinstance(value, outer if outer is not tuple else (list, tuple)):
            return None, "%s should be a %s" % (name, outer.__name__)
        return _coerce_inside(value, wanted, outer, name)
    return value, ""


def _coerce_inside(value, wanted, outer, name):
    """Convert what is inside a List[...] or Dict[...], not just the outside.

    Skipped for a long time on the grounds that checking the inside of a
    generic is a rabbit hole. It is, for *validation*. For *loading* it is the
    whole job: QuickBackupM declares `Dict[str, SlotInfo]`, and a loader that
    stops at the outside hands the plugin a dict of dicts, which blows up as
    `'dict' object has no attribute 'delete_protection'` the moment it reads
    one. It only showed up on a reload, because the first load had no file.

    An element that does not fit is dropped and reported, rather than losing
    the whole collection: one corrupt backup slot should not cost somebody the
    other twenty.
    """
    args = [a for a in getattr(wanted, "__args__", ()) or ()
            if a is not Ellipsis and not isinstance(a, type(None))]
    if not args:
        return value, ""
    problems = []
    if outer is dict:
        inner = args[-1]
        if not _worth_coercing(inner):
            return value, ""
        out = {}
        for key, item in value.items():
            fixed, why = _coerce(item, inner, "%s[%s]" % (name, key))
            if why:
                problems.append(why)
                continue
            out[key] = fixed
        return out, "; ".join(problems)
    inner = args[0]
    if not _worth_coercing(inner):
        return value, ""
    out = []
    for index, item in enumerate(value):
        fixed, why = _coerce(item, inner, "%s[%d]" % (name, index))
        if why:
            problems.append(why)
            continue
        out.append(fixed)
    if outer is set:
        return set(out), "; ".join(problems)
    if outer is tuple:
        return tuple(out), "; ".join(problems)
    return out, "; ".join(problems)


def _is_pattern_type(wanted):
    """Is this field declared as a compiled regex?

    Both spellings count: `re.Pattern`, and the `typing.Pattern` alias that
    older plugins still use.
    """
    if wanted is re.Pattern:
        return True
    return getattr(wanted, "__origin__", None) is re.Pattern


def _worth_coercing(inner):
    """Only convert element types that actually need converting.

    `List[str]` is already a list of strings once JSON is parsed, and running
    every element through the type checker would turn one bad line into a
    dropped element in configs that have worked for years.
    """
    return isinstance(inner, type) and issubclass(inner, Serializable)


def serialize(obj):
    """`serialize(anything)`: the module level version plugins import.

    Task imports this from `mcdreforged.api.utils` rather than calling the
    method, and the two have to agree, so this is the same code path.
    """
    return _serialize_value(obj)


def deserialize(data, cls, **kwargs):
    """`deserialize(data, SomeClass)`: build an object out of plain JSON.

    The second argument is named `cls` because plugins pass it by keyword:
    Task calls `deserialize(data=data, cls=cls)` from inside its own
    classmethod, and a parameter named anything else is a TypeError at load.
    """
    if isinstance(cls, type) and issubclass(cls, Serializable):
        instance, problems = _build(cls, data)
        if kwargs.get("with_problems"):
            return instance, problems
        return instance
    value, problem = _coerce(data, cls, "value")
    if problem:
        raise ValueError(problem)
    return value
