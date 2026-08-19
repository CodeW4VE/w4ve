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
            value = getattr(self, name, None)
            out[name] = value.serialize() if isinstance(value, Serializable) else value
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
    # A typing generic like List[str] or Dict[str, int]. Checking the inside
    # of one is a rabbit hole; checking the outside catches the real mistakes.
    outer = getattr(wanted, "__origin__", None)
    if outer in (list, dict, set, tuple):
        if not isinstance(value, outer):
            return None, "%s should be a %s" % (name, outer.__name__)
        return value, ""
    return value, ""
