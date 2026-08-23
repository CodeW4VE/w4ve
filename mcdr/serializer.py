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

_MISSING = object()


class Serializable:
    """Base class for typed config and payload objects."""

    def __init__(self, **fields):
        for name, default in self.__field_defaults().items():
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
        defaults = {}
        for name in cls.__field_types():
            value = getattr(cls, name, _MISSING)
            if value is not _MISSING and not callable(value):
                defaults[name] = value
            else:
                defaults[name] = None
        return defaults

    @classmethod
    def get_field_annotations(cls):
        return dict(cls.__field_types())

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
        instance = cls()
        problems = []
        types = cls.__field_types()
        for name, wanted in types.items():
            if name not in (data or {}):
                continue
            value = data[name]
            fixed, why = _coerce(value, wanted, name)
            if why:
                problems.append(why)
                # A partial result is still a result. A collection that lost
                # one bad element keeps the other nineteen; a value that could
                # not be read at all comes back as None and keeps the default.
                if fixed is None:
                    continue
            setattr(instance, name, fixed)
        for name in (data or {}):
            if name not in types:
                problems.append("%s is not a setting this plugin reads" % name)
        return instance, problems

    # --------------------------------------------------------------- writing

    def serialize(self):
        out = {}
        for name in type(self).__field_types():
            out[name] = _serialize_value(getattr(self, name, None))
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
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _coerce(value, wanted, name):
    """Make a JSON value fit a declared type, or say why it does not."""
    if wanted is None or wanted is type(None):
        return value, ""
    if isinstance(wanted, type) and issubclass(wanted, Serializable):
        if not isinstance(value, dict):
            return None, "%s should be a block of settings" % name
        return wanted.deserialize(value), ""
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


def deserialize(data, target_class, **kwargs):
    """`deserialize(data, SomeClass)`: build an object out of plain JSON."""
    if isinstance(target_class, type) and issubclass(target_class, Serializable):
        return target_class.deserialize(data, **kwargs)
    value, problem = _coerce(data, target_class, "value")
    if problem:
        raise ValueError(problem)
    return value
