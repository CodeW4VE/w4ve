"""RText: the text objects MCDR plugins build their output with.

Every plugin measured uses these, and they are the reason a plugin's output is
clickable and hoverable rather than a string. Minecraft's own format is a JSON
tree; RText is a small builder on top of it, and this is that builder
reimplemented from its documented behaviour.

Never copied. MCDR is LGPL-3.0, and code taken from it would pull that licence
onto W4VE. What is reproduced here is the shape of the API, which is what
compatibility means.

The one rule worth stating: a plugin may hold an RText and mutate it later, and
`RText.h()` and `.c()` return the same object rather than a copy, because
plugins chain them and then keep the result.
"""

import json


class RColor:
    """The sixteen names Minecraft has, as MCDR spells them."""

    black = "black"
    dark_blue = "dark_blue"
    dark_green = "dark_green"
    dark_aqua = "dark_aqua"
    dark_red = "dark_red"
    dark_purple = "dark_purple"
    gold = "gold"
    gray = "gray"
    dark_gray = "dark_gray"
    blue = "blue"
    green = "green"
    aqua = "aqua"
    red = "red"
    light_purple = "light_purple"
    yellow = "yellow"
    white = "white"
    reset = "reset"


class RStyle:
    bold = "bold"
    italic = "italic"
    underlined = "underlined"
    strikethrough = "strikethrough"
    obfuscated = "obfuscated"


class RAction:
    """What a click does. The names are Minecraft's own."""

    suggest_command = "suggest_command"
    run_command = "run_command"
    open_url = "open_url"
    open_file = "open_file"
    copy_to_clipboard = "copy_to_clipboard"


class RTextBase:
    """Anything that can end up as a chat component."""

    def to_json_object(self):
        raise NotImplementedError

    def to_json_str(self):
        return json.dumps(self.to_json_object(), ensure_ascii=False)

    def to_plain_text(self):
        raise NotImplementedError

    def __str__(self):
        return self.to_plain_text()

    def __add__(self, other):
        return RTextList(self, other)

    def __radd__(self, other):
        return RTextList(other, self)

    @staticmethod
    def from_any(value):
        return value if isinstance(value, RTextBase) else RText(value)


class RText(RTextBase):
    """One piece of text with a colour, some styles and maybe an action."""

    def __init__(self, text, color=None, styles=None):
        self.text = text if isinstance(text, str) else str(text)
        self.color = color
        if styles is None:
            self.styles = []
        elif isinstance(styles, str):
            self.styles = [styles]
        else:
            self.styles = list(styles)
        self.click_action = None
        self.click_value = None
        self.hover_text = None

    # ------------------------------------------------------------- building

    def set_color(self, color):
        self.color = color
        return self

    def set_styles(self, styles):
        self.styles = [styles] if isinstance(styles, str) else list(styles)
        return self

    def set_click_event(self, action, value):
        self.click_action, self.click_value = action, value
        return self

    def set_hover_text(self, *args):
        self.hover_text = (RTextList(*args) if len(args) > 1
                           else RTextBase.from_any(args[0]))
        return self

    # The short names plugins actually type. Here uses `coord.h(...).c(...)`.
    c = set_click_event
    h = set_hover_text

    def copy(self):
        clone = RText(self.text, self.color, list(self.styles))
        clone.click_action, clone.click_value = self.click_action, self.click_value
        clone.hover_text = self.hover_text
        return clone

    # -------------------------------------------------------------- output

    def to_json_object(self):
        data = {"text": self.text}
        if self.color:
            data["color"] = self.color
        for style in self.styles:
            data[style] = True
        if self.click_action:
            data["clickEvent"] = {"action": self.click_action,
                                  "value": self.click_value}
        if self.hover_text is not None:
            data["hoverEvent"] = {"action": "show_text",
                                  "contents": self.hover_text.to_json_object()}
        return data

    def to_plain_text(self):
        return self.text


class RTextList(RTextBase):
    """Several pieces in a row. Plugins build most of their output this way."""

    def __init__(self, *children):
        self.children = []
        for child in children:
            self.append(child)

    def append(self, *children):
        for child in children:
            if isinstance(child, RTextList):
                # Flatten: a nested list renders the same and reads better in
                # the JSON a server ends up sending.
                self.children.extend(child.children)
            else:
                self.children.append(RTextBase.from_any(child))
        return self

    def is_empty(self):
        return not self.children

    def to_json_object(self):
        # An empty first element is the standard trick to stop Minecraft
        # inheriting the first child's formatting for the whole list.
        return [""] + [child.to_json_object() for child in self.children]

    def to_plain_text(self):
        return "".join(child.to_plain_text() for child in self.children)


class RTextTranslation(RText):
    """A key the client translates, used by plugins that ship languages."""

    def __init__(self, key, color=None, styles=None):
        RText.__init__(self, key, color, styles)
        self.key = key

    def to_json_object(self):
        data = RText.to_json_object(self)
        data.pop("text", None)
        data["translate"] = self.key
        return data


class RTextMCDRTranslation(RText):
    """MCDR's own translation object, which resolves against the host.

    PrimeBackup builds most of its output from these. Without a language file
    loaded, the honest thing is to show the key rather than a blank line.
    """

    def __init__(self, key, *args, **kwargs):
        RText.__init__(self, key)
        self.key = key
        self.args = args
        self.kwargs = kwargs
        self._translator = None

    def set_translator(self, translator):
        self._translator = translator
        return self

    def to_plain_text(self):
        if self._translator is None:
            return self.key
        return self._translator(self.key, *self.args, **self.kwargs)

    def to_json_object(self):
        data = RText.to_json_object(self)
        data["text"] = self.to_plain_text()
        return data
