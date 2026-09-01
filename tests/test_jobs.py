"""Work that answers in minutes, over the API (the jobs of M8).

Everything the API did until now answered at once or failed at once, which
leaves out the operation people actually want to run from outside: a backup.
`!!pb make` comes back four minutes later, and by then the request that asked
for it is long gone.

So this is checked against a guardian and a server that really exist, with a
console that really takes its time, because a job that only works against a
mock is a job that has never met a slow answer.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk" / "client"))

import api  # noqa: E402
import runtime  # noqa: E402
from w4ve_client import W4VE, Denied  # noqa: E402

GUARD = str(HERE / "guard.py")
FAKE = str(HERE / "fakeserver.py")


def wait_for(check, seconds=15, every=0.05):
    limit = time.time() + seconds
    while time.time() < limit:
        answer = check()
        if answer:
            return answer
        time.sleep(every)
    return None


class JobCase(unittest.TestCase):
    """The API lives outside the guardian here, which is the harder case.

    It has to follow the console over the control socket like any other
    client, so nothing in the job can quietly depend on being in-process.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-jobs-"))
        (self.tmp / "w4ve" / "run").mkdir(parents=True)
        argv = [sys.executable, GUARD, str(self.tmp), "--",
                sys.executable, FAKE, "--boot", "0.2"]
        self.guard = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,
                                      universal_newlines=True)
        control = runtime.Client(self.tmp)
        self.assertTrue(wait_for(lambda: control.call({"cmd": "ping"}).get("ok")),
                        "the guardian never answered its socket")
        self.assertTrue(
            wait_for(lambda: (control.call({"cmd": "status"})
                              .get("processes", [{}])[0].get("ready"))),
            "the server never came up")
        self.api = api.Api(self.tmp)          # no guardian= : over the socket
        ok, message = self.api.start()
        self.assertTrue(ok, message)
        self.client = W4VE(self.tmp)

    def tearDown(self):
        self.api.stop()
        self.guard.terminate()
        try:
            self.guard.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.guard.kill()

    # ------------------------------------------------------------- the point

    def test_a_slow_job_is_accepted_now_and_answered_later(self):
        started = self.client.start_job("slowjob 2", until=r"\[Backup\] done",
                                        timeout=30)
        self.assertEqual(started["state"], "running")
        self.assertTrue(started["id"])

        # It really is not finished yet: that is the whole reason jobs exist.
        self.assertEqual(self.client.job(started["id"])["state"], "running")

        done = self.client.wait_for_job(started["id"], every=0.2, timeout=30)
        self.assertEqual(done["state"], "done", done.get("error"))
        self.assertIn("[Backup] done", done["matched"])
        self.assertTrue(any("[Backup] starting" in line for line in done["lines"]),
                        "the first line of the answer was lost")

    def test_the_line_that_arrives_immediately_is_not_missed(self):
        """Subscribe first, send second. The other order loses short answers."""
        done = self.client.wait_for_job(
            self.client.start_job("hello", until=r"Unknown or incomplete",
                                  timeout=15)["id"], every=0.1, timeout=20)
        self.assertEqual(done["state"], "done", done.get("error"))
        self.assertIn("hello", done["matched"])

    def test_waiting_for_something_that_never_comes_fails_and_says_so(self):
        done = self.client.wait_for_job(
            self.client.start_job("hello", until=r"THIS NEVER HAPPENS",
                                  timeout=1)["id"], every=0.1, timeout=20)
        self.assertEqual(done["state"], "failed")
        self.assertIn("never came", done["error"])
        # It still kept what it heard, which is what you read to find out why.
        self.assertTrue(done["lines"])

    def test_with_nothing_to_wait_for_the_deadline_is_the_plan(self):
        done = self.client.wait_for_job(
            self.client.start_job("hello", timeout=1)["id"],
            every=0.1, timeout=20)
        self.assertEqual(done["state"], "done", done.get("error"))
        self.assertEqual(done["error"], "", "a deadline with no target is not a failure")

    def test_cancelling_stops_listening_and_admits_it_stops_nothing_else(self):
        job = self.client.start_job("slowjob 5", until=r"\[Backup\] done",
                                    timeout=60)
        answer = self.client.cancel_job(job["id"])
        self.assertEqual(answer["state"], "cancelled")
        self.assertIn("carries on", answer["message"],
                      "cancelling must not pretend the work was undone")
        self.assertEqual(self.client.job(job["id"])["state"], "cancelled")

    def test_cancelling_a_finished_job_is_refused_rather_than_pretended(self):
        job = self.client.start_job("hello", until=r"Unknown", timeout=15)
        self.client.wait_for_job(job["id"], every=0.1, timeout=20)
        with self.assertRaises(Exception) as caught:
            self.client.cancel_job(job["id"])
        self.assertIn("already finished", str(caught.exception))

    def test_the_listing_shows_them_without_the_lines(self):
        job = self.client.start_job("hello", until=r"Unknown", timeout=15)
        self.client.wait_for_job(job["id"], every=0.1, timeout=20)
        listing = self.client.jobs()
        mine = [j for j in listing if j["id"] == job["id"]]
        self.assertEqual(len(mine), 1)
        self.assertIsInstance(mine[0]["lines"], int,
                              "the listing carries a count, not the console")

    # --------------------------------------------------------- saying no well

    def test_a_job_that_does_not_exist_is_a_404_not_an_empty_job(self):
        code, payload = self.api.run(("GET", "/v1/jobs/{id}"), {}, "nope")
        self.assertEqual(code, 404)
        self.assertFalse(payload["ok"])

    def test_a_broken_until_is_refused_at_the_door(self):
        code, payload = self.api.run(("POST", "/v1/jobs"),
                                     {"command": "hello", "until": "[unclosed"})
        self.assertEqual(code, 400)
        self.assertIn("regular expression", payload["error"])

    def test_a_job_with_no_command_is_refused(self):
        code, payload = self.api.run(("POST", "/v1/jobs"), {})
        self.assertEqual(code, 400)

    def test_an_absurd_timeout_is_capped_and_not_refused(self):
        code, payload = self.api.run(("POST", "/v1/jobs"),
                                     {"command": "hello", "timeout": 999999})
        self.assertEqual(code, 202)
        self.assertEqual(payload["timeout"], api.JOB_MAX_SECONDS)
        self.api.jobs.get(payload["id"]).cancel()

    def test_starting_a_job_needs_more_than_a_reading_token(self):
        self.api.tokens_required = True
        try:
            token = self.api.tokens.issue("dashboard", "read")
            reader = W4VE(self.tmp, token=token)
            with self.assertRaises(Denied):
                reader.start_job("slowjob 1")
            reader.jobs()          # but looking is fine
        finally:
            self.api.tokens_required = False


if __name__ == "__main__":
    unittest.main()
