"""Verify the compiled companion's real w4ve1 proof in W4VE's authority."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from identity import Authority, IdentityDenied
from pairing import configure
from w4ve import Server


def check(java, classpath):
    with tempfile.TemporaryDirectory(prefix="java-w4ve-identity-") as temp:
        root = Path(temp)
        (root / "server").mkdir()
        (root / "w4ve.toml").write_text('[server]\nkind="server"\ngame_dir="server"\n')
        server = Server(root)
        spec = {"audience": "wavechat", "enabled_setting": "native_chat", "url_setting": "public_url",
                "consumer_config": "w4ve/service/chat/native.json", "issuer_config": "config/wavechat/w4ve-identity.json",
                "resources": [{"id": "channel:general", "name": "General", "operations": ["chat.read", "chat.publish"]}]}
        binding = configure(server, "wavechat-server", spec, {"native_chat": True, "public_url": "https://chat.example.test"})
        proof_file = root / "proof.txt"
        fd = os.open(proof_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        now = int(time.time())
        result = subprocess.run([str(java), "-cp", classpath, "IdentityProofCheck",
                                 str(Path(binding["issuer_config"]).parent), str(proof_file),
                                 "466a0fbd-7b87-431c-ab58-c1887f577deb", str(now)],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AssertionError("Java companion check failed; credential-bearing output suppressed")
        proof = proof_file.read_text()
        authority = Authority(root / "w4ve/identity/authority.sqlite3")
        session = authority.create_session(proof, "wavechat")["token"]
        claims = authority.inspect(session, "wavechat")
        assert claims["subject"] == "minecraft:466a0fbd7b87431cab58c1887f577deb"
        assert claims["community"] == binding["community"] and claims["origin"] == binding["instance"]
        assert authority.authorize(session, "wavechat", "channel:general", "chat.publish")["allowed"]
        assert not authority.authorize(session, "wavechat", "channel:private", "chat.read")["allowed"]
        for note in (proof, proof[:-1] + ("A" if proof[-1] != "A" else "B"), proof.replace("w4ve1", "wc1", 1)):
            try:
                authority.create_session(note, "wavechat")
            except IdentityDenied:
                pass
            else:
                raise AssertionError("replayed/tampered/legacy proof accepted")
        authority.set_grants(binding["community"], "wavechat", "channel:general", "*", [])
        assert not authority.authorize(session, "wavechat", "channel:general", "chat.publish")["allowed"]
        return {"java_companion_proof": "passed", "canonical_identity": "passed", "grant_revoke": "passed",
                "replay_tamper_wrong_protocol": "passed", "real_minecraft_connection": "not tested"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--classpath", required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.java, args.classpath), sort_keys=True))
