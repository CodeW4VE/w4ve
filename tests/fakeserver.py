"""A Minecraft server that is not one, for testing the guardian.

It boots slowly, prints the same lines a real one prints, listens on stdin and
can be told to misbehave: crash on its own, ignore `stop`, or ignore SIGTERM.
Those three are the cases that matter and the ones you cannot rehearse against
a real world without risking it.
"""

import argparse
import select
import signal
import sys
import time


def out(line):
    print(line, flush=True)


def stamp(text, thread="Server thread"):
    return "[%s] [%s/INFO]: %s" % (time.strftime("%H:%M:%S"), thread, text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=float, default=0.5, help="seconds until ready")
    ap.add_argument("--crash-after", type=float, help="die on its own, exit 1")
    ap.add_argument("--ignore-stop", action="store_true")
    ap.add_argument("--ignore-term", action="store_true")
    ap.add_argument("--flavor", default="fabric")
    ap.add_argument("--ask-restart-after", type=float,
                    help="print a piece's restart marker and exit, like RegionCast")
    args = ap.parse_args()

    if args.ignore_term:
        signal.signal(signal.SIGTERM, lambda *_: out(stamp("nope")))

    out(stamp("Starting minecraft server version 1.21"))
    out(stamp("Loading properties", "ServerMain"))
    time.sleep(args.boot)
    out(stamp('Done (%.3fs)! For help, type "help"' % args.boot))

    started = time.time()
    # Work that answers minutes later, which is the shape of a backup: the
    # command comes back at once and the result arrives whenever it arrives.
    slow_done_at = None
    while True:
        if slow_done_at is not None and time.time() > slow_done_at:
            slow_done_at = None
            out(stamp("[Backup] done, 1 file"))
        if args.crash_after and time.time() - started > args.crash_after:
            out(stamp("something went very wrong"))
            sys.exit(1)
        if (args.ask_restart_after
                and time.time() - started > args.ask_restart_after):
            out(stamp("[RegionCast] copying region files"))
            out(stamp("[RegionCast] RESTART_REQUESTED"))
            sys.exit(0)
        # Never block on stdin: a server that only reacts when someone types
        # would never get around to crashing on its own.
        if not select.select([sys.stdin], [], [], 0.1)[0]:
            continue
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.1)
            continue
        command = line.strip()
        if command.startswith("slowjob"):
            bits = command.split()
            wait = float(bits[1]) if len(bits) > 1 else 1.0
            slow_done_at = time.time() + wait
            out(stamp("[Backup] starting"))
            continue
        if command == "stop" and not args.ignore_stop:
            out(stamp("Stopping server"))
            out(stamp("Saving worlds"))
            time.sleep(0.3)
            sys.exit(0)
        out(stamp("Unknown or incomplete command: %s" % command))


if __name__ == "__main__":
    main()
