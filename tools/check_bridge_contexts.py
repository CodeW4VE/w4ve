"""Check shared contexts, optional absence and incompatible API behavior in JVMs."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def check(args):
    root = Path(__file__).resolve().parents[1]
    javac, java = (str(args.java_home / "bin" / name) for name in ("javac", "java"))
    with tempfile.TemporaryDirectory(prefix="w4ve-contexts-") as temporary:
        scratch = Path(temporary)
        classes = scratch / "classes"
        source = root / "bridge/src/main/java/xyz/w4ve/bridge/ConnectionContexts.java"
        subprocess.run([javac, "-d", str(classes), str(source),
                        str(root.parent / "WaveMotes/src/main/java/xyz/w4ve/wavemotes/server/PlatformConnections.java"),
                        str(root / "tools/BridgeContextsCheck.java")], check=True, timeout=30)
        subprocess.run([java, "-cp", str(classes), "BridgeContextsCheck"], check=True, timeout=10)
        registry = classes / "xyz/w4ve/bridge/ConnectionContexts.class"
        registry.rename(scratch / "registry.class")
        subprocess.run([java, "-cp", str(classes), "BridgeContextsCheck", "--standalone"], check=True, timeout=10)
        broken = scratch / "ConnectionContexts.java"
        broken.write_text("package xyz.w4ve.bridge; public final class ConnectionContexts {}\n")
        subprocess.run([javac, "-d", str(classes), str(broken)], check=True, timeout=30)
        subprocess.run([java, "-cp", str(classes), "BridgeContextsCheck", "--broken"], check=True, timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    check(parser.parse_args())
