"""Tests for the command tree.

The shapes tested are the ones the real plugins build. DigAll nests two
literals under a prefix and runs a callback at each level. StatsHelper defines
its own node types by subclassing ArgumentNode, and leans on `on_error` and
`on_child_error` to answer a typo with a clickable suggestion. PrimeBackup uses
the string based builder rather than nesting nodes by hand.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.command import (ArgumentNode, Boolean, CommandContext,  # noqa: E402
                          CommandError, CountingLiteral, Enumeration, Float,
                          GreedyText, Integer, InvalidBoolean, InvalidInteger,
                          IllegalArgument, Literal, NumberOutOfRange,
                          ParseResult, QuotableText, RequirementNotMet,
                          SimpleCommandBuilder, Text, UnclosedQuotedString,
                          UnknownArgument, UnknownCommand)


class Source:
    """Stands in for whoever typed the command."""

    def __init__(self, level=1):
        self.replies = []
        self.level = level

    def reply(self, text, **kwargs):
        self.replies.append(str(text))

    def get_permission_level(self):
        return self.level


class TreeCase(unittest.TestCase):

    def setUp(self):
        self.source = Source()
        self.ran = []

    def test_the_shape_digall_builds(self):
        tree = (Literal("!!dig-all")
                .runs(lambda src: self.ran.append("show"))
                .then(Literal("reseed").runs(lambda src: self.ran.append("reseed")))
                .then(Literal("status").runs(lambda src: self.ran.append("status"))))
        tree.execute(self.source, "!!dig-all")
        tree.execute(self.source, "!!dig-all status")
        tree.execute(self.source, "!!dig-all reseed")
        self.assertEqual(self.ran, ["show", "status", "reseed"])

    def test_arguments_reach_the_callback_by_name(self):
        seen = {}
        tree = (Literal("!!give")
                .then(Text("player")
                      .then(Integer("amount")
                            .runs(lambda src, ctx: seen.update(ctx)))))
        tree.execute(self.source, "!!give TVTvirus 64")
        self.assertEqual(seen["player"], "TVTvirus")
        self.assertEqual(seen["amount"], 64)
        self.assertIsInstance(seen["amount"], int)

    def test_a_callback_can_take_one_argument_or_two(self):
        """Plugins write both, so the tree has to look at the signature."""
        got = []
        one = Literal("a").runs(lambda src: got.append("one"))
        two = Literal("b").runs(lambda src, ctx: got.append("two"))
        one.execute(self.source, "a")
        two.execute(self.source, "b")
        self.assertEqual(got, ["one", "two"])

    def test_greedy_text_takes_the_rest_of_the_line(self):
        seen = {}
        tree = Literal("!!say").then(GreedyText("message")
                                     .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, "!!say hello there, everyone")
        self.assertEqual(seen["message"], "hello there, everyone")

    def test_quoted_text(self):
        seen = {}
        tree = Literal("!!t").then(QuotableText("title")
                                   .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, '!!t "two words"')
        self.assertEqual(seen["title"], "two words")

    def test_an_unclosed_quote_is_an_error_not_a_crash(self):
        tree = Literal("!!t").then(QuotableText("title").runs(lambda src: None))
        with self.assertRaises(UnclosedQuotedString):
            tree.execute(self.source, '!!t "never closed')

    def test_a_number_out_of_range(self):
        tree = Literal("!!n").then(Integer("n").in_range(1, 10)
                                   .runs(lambda src: None))
        tree.execute(self.source, "!!n 5")
        with self.assertRaises(NumberOutOfRange):
            tree.execute(self.source, "!!n 99")

    def test_text_that_is_not_a_number(self):
        tree = Literal("!!n").then(Integer("n").runs(lambda src: None))
        with self.assertRaises(InvalidInteger):
            tree.execute(self.source, "!!n banana")

    def test_booleans(self):
        seen = {}
        tree = Literal("!!b").then(Boolean("on")
                                   .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, "!!b true")
        self.assertTrue(seen["on"])
        tree.execute(self.source, "!!b off")
        self.assertFalse(seen["on"])
        with self.assertRaises(InvalidBoolean):
            tree.execute(self.source, "!!b maybe")

    def test_an_enumeration(self):
        seen = {}
        tree = Literal("!!mode").then(
            Enumeration("mode", {"survival": 0, "creative": 1})
            .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, "!!mode creative")
        self.assertEqual(seen["mode"], 1)

    def test_a_word_with_no_branch_is_an_unknown_argument(self):
        tree = Literal("!!x").then(Literal("known").runs(lambda src: None))
        with self.assertRaises(UnknownArgument):
            tree.execute(self.source, "!!x nonsense")

    def test_a_path_that_runs_nothing_says_so(self):
        tree = Literal("!!x").then(Literal("half"))
        with self.assertRaises(UnknownCommand):
            tree.execute(self.source, "!!x half")

    def test_a_line_for_somebody_else_is_left_alone(self):
        tree = Literal("!!mine").runs(lambda src: self.ran.append("mine"))
        self.assertFalse(tree._could_match("!!theirs something"))
        self.assertEqual(self.ran, [])

    # ------------------------------------------------------------ permissions

    def test_requires_stops_the_command_and_explains(self):
        tree = (Literal("!!admin")
                .requires(lambda src: src.get_permission_level() >= 3,
                          lambda src: "you are not an admin")
                .runs(lambda src: self.ran.append("ran")))
        with self.assertRaises(RequirementNotMet) as caught:
            tree.execute(Source(level=1), "!!admin")
        self.assertEqual(caught.exception.get_reason(), "you are not an admin")
        self.assertEqual(self.ran, [])
        tree.execute(Source(level=4), "!!admin")
        self.assertEqual(self.ran, ["ran"])

    # ---------------------------------------------------------------- errors

    def test_the_error_handling_statshelper_relies_on(self):
        """A typo answers with a suggestion instead of a stack trace."""
        source = Source()
        tree = (Literal("!!stats")
                .runs(lambda src: src.reply("help"))
                .on_error(UnknownArgument,
                          lambda src: src.reply("unknown argument, try !!stats"),
                          handled=True)
                .then(Literal("rank").runs(lambda src: src.reply("rank"))))
        tree.execute(source, "!!stats typo")
        self.assertEqual(source.replies, ["unknown argument, try !!stats"])

    def test_on_child_error_catches_what_a_branch_raised(self):
        source = Source()

        def explode(src, ctx):
            raise IllegalArgument("bad scoreboard")

        tree = (Literal("!!stats")
                .on_child_error(IllegalArgument,
                                lambda src: src.reply("unknown scoreboard"),
                                handled=True)
                .then(Text("name").runs(explode)))
        tree.execute(source, "!!stats nope")
        self.assertEqual(source.replies, ["unknown scoreboard"])

    # ------------------------------------------------------- custom nodes

    def test_a_plugin_can_write_its_own_node(self):
        """StatsHelper subclasses ArgumentNode, so parse is a real contract."""

        class Coordinate(ArgumentNode):
            def parse(self, text):
                head = text.split(" ", 1)[0]
                parts = head.split(",")
                if len(parts) != 3:
                    raise IllegalArgument("%r is not x,y,z" % head)
                try:
                    value = tuple(int(p) for p in parts)
                except ValueError:
                    raise IllegalArgument("%r is not numbers" % head)
                return ParseResult(value, len(head))

        seen = {}
        tree = Literal("!!tp").then(Coordinate("where")
                                    .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, "!!tp 10,64,-30")
        self.assertEqual(seen["where"], (10, 64, -30))
        with self.assertRaises(IllegalArgument):
            tree.execute(self.source, "!!tp nope")

    def test_a_counting_literal(self):
        seen = {}
        tree = Literal("!!x").then(CountingLiteral("--all", "all")
                                   .runs(lambda src, ctx: seen.update(ctx)))
        tree.execute(self.source, "!!x --all")
        self.assertEqual(seen["all"], 1)


class BuilderCase(unittest.TestCase):
    """The string based shorthand PrimeBackup builds its commands with."""

    def setUp(self):
        self.source = Source()
        self.ran = []

    def test_paths_become_one_tree(self):
        builder = SimpleCommandBuilder()
        builder.command("!!pb", lambda src: self.ran.append("root"))
        builder.command("!!pb list", lambda src: self.ran.append("list"))
        builder.command("!!pb back <id>", lambda src, ctx: self.ran.append(ctx["id"]))
        builder.arg("id", Integer)
        roots = builder.build()
        self.assertEqual(len(roots), 1, "one root word, one tree")
        root = roots[0]
        root.execute(self.source, "!!pb")
        root.execute(self.source, "!!pb list")
        root.execute(self.source, "!!pb back 42")
        self.assertEqual(self.ran, ["root", "list", 42])

    def test_branches_that_share_a_prefix_share_a_node(self):
        builder = SimpleCommandBuilder()
        builder.command("!!pb make full", lambda src: self.ran.append("full"))
        builder.command("!!pb make quick", lambda src: self.ran.append("quick"))
        root = builder.build()[0]
        self.assertEqual(len(root.children), 1, "`make` must not be duplicated")
        root.execute(self.source, "!!pb make quick")
        root.execute(self.source, "!!pb make full")
        self.assertEqual(self.ran, ["quick", "full"])

    def test_an_undeclared_argument_is_plain_text(self):
        seen = {}
        builder = SimpleCommandBuilder()
        builder.command("!!x <thing>", lambda src, ctx: seen.update(ctx))
        builder.build()[0].execute(self.source, "!!x whatever")
        self.assertEqual(seen["thing"], "whatever")


class ContextCase(unittest.TestCase):

    def test_a_context_is_a_dict_with_a_source(self):
        source = Source()
        context = CommandContext(source, "!!x y")
        context["player"] = "TVTvirus"
        # StatsHelper reads a key with a slash in it, so nothing may wrap keys.
        context["cls/alias"] = ("minecraft:mined", "stone")
        clone = context.copy()
        self.assertEqual(clone["cls/alias"], ("minecraft:mined", "stone"))
        self.assertIs(clone.source, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
