import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.UUID;
import xyz.w4ve.bridge.CaptureSpool;
import xyz.w4ve.bridge.LocalSource;

/** Crash boundaries and pressure, using only the dedicated fixture directories. */
public final class BridgeSpoolCheck {
    private static String id() { return UUID.randomUUID().toString().replace("-", ""); }
    private static JsonObject chat(String text) {
        var data = new JsonObject();
        data.addProperty("uuid", "a".repeat(32)); data.addProperty("connection", "b".repeat(32));
        data.addProperty("text", text); data.addProperty("signature_sha256", "");
        return data;
    }
    public static void main(String[] args) throws Exception {
        if (args[0].equals("--try-lock")) {
            Path target = Path.of(args[1]);
            try (var ignored = new CaptureSpool(target, LocalSource.load(target))) { System.exit(2); }
            catch (IOException locked) { System.exit(0); }
        }
        Path descriptor = Path.of(args[0]);
        var source = LocalSource.load(descriptor);
        long now = System.currentTimeMillis()/1000;
        Path capture;
        try (var spool = new CaptureSpool(descriptor, source)) {
            var start = spool.append(id(), "server.started", now, new JsonObject());
            if (start.sequence() != 1) throw new AssertionError("first capture sequence");
            spool.acknowledge(start);
            var pending = spool.append(id(), "player.chat", now, chat("PRIVATE_FIXTURE"));
            if (pending.sequence() != 2 || pending.toString().contains("PRIVATE_FIXTURE"))
                throw new AssertionError("capture metadata leaks or loses sequence");
            capture = pending.path();
            try { new CaptureSpool(descriptor, source).close(); throw new AssertionError("concurrent spool owner accepted"); }
            catch (IOException expected) { }
            var contender = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-cp", System.getProperty("java.class.path"), "BridgeSpoolCheck", "--try-lock", descriptor.toString()).start();
            if (!contender.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                contender.destroyForcibly(); contender.waitFor();
                throw new AssertionError("spool lock contender did not return");
            }
            contender.getInputStream().close(); contender.getErrorStream().close(); contender.getOutputStream().close();
            if (contender.exitValue() != 0) throw new AssertionError("OS spool lock was lost after same-JVM contention");
        }
        try (var spool = new CaptureSpool(descriptor, source)) {
            var pending = spool.pending();
            if (pending.size() != 1 || !pending.getFirst().event().getAsJsonObject("data").get("text").getAsString().equals("PRIVATE_FIXTURE"))
                throw new AssertionError("durable capture did not survive reopen");
            spool.acknowledge(pending.getFirst());
        }
        try (var spool = new CaptureSpool(descriptor, source)) {
            var next = spool.append(id(), "player.chat", now, chat("PRIVATE_FIXTURE"));
            if (next.sequence() != 3) throw new AssertionError("ack deletion reset sequence");
            capture = next.path();
        }
        // A complete fsynced staging file may survive a crash before rename;
        // the older counter must not cause an event/sequence to be reused.
        Path staging = capture.resolveSibling(".capture-" + capture.getFileName() + "-123.tmp");
        Files.move(capture, staging, StandardCopyOption.ATOMIC_MOVE);
        Path sequenceFile = capture.getParent().resolve("sequence.json");
        var old = JsonParser.parseString(Files.readString(sequenceFile)).getAsJsonObject();
        old.addProperty("sequence", 1);
        Files.writeString(sequenceFile, old.toString());
        try (var spool = new CaptureSpool(descriptor, source)) {
            if (!Files.exists(capture) || Files.exists(staging)) throw new AssertionError("durable staging was not recovered");
            var next = spool.append(id(), "player.left", now, new JsonObject());
            if (next.sequence() != 4) throw new AssertionError("recovery did not preserve highest sequence");
            Path foreign = descriptor.getParent().resolve("foreign.txt");
            Files.writeString(foreign, "preserve");
            try { spool.acknowledge(new CaptureSpool.Pending(foreign, next.event(), source)); throw new AssertionError("receipt escaped spool"); }
            catch (IOException expected) { }
            if (!Files.readString(foreign).equals("preserve")) throw new AssertionError("foreign file was changed");
        }
        byte[] original = Files.readAllBytes(capture);
        var corrupt = JsonParser.parseString(new String(original, java.nio.charset.StandardCharsets.UTF_8)).getAsJsonObject();
        corrupt.getAsJsonObject("event").getAsJsonObject("data").addProperty("text", "changed");
        Files.writeString(capture, corrupt.toString());
        try { new CaptureSpool(descriptor, source).close(); throw new AssertionError("corrupt capture accepted"); }
        catch (IOException expected) { }
        Files.write(capture, original);
        try (var spool = new CaptureSpool(descriptor, source)) {
            try { spool.expire(now + 3601); throw new AssertionError("expired stream did not require reconciliation"); }
            catch (CaptureSpool.Expired expected) { }
            if (!spool.pending().isEmpty()) throw new AssertionError("expired private captures retained");
            try { spool.append(id(), "server.stopping", now + 3601, new JsonObject()); throw new AssertionError("expired stream resumed silently"); }
            catch (IOException expected) { }
        }
        Path quotaDescriptor = Path.of(args[1]);
        var quotaSource = LocalSource.load(quotaDescriptor);
        try (var spool = new CaptureSpool(quotaDescriptor, quotaSource, 2, CaptureSpool.MAX_BYTES)) {
            spool.append(id(), "server.started", now, new JsonObject());
            spool.append(id(), "server.stopping", now, new JsonObject());
            try { spool.append(id(), "server.stopping", now, new JsonObject()); throw new AssertionError("event quota bypassed"); }
            catch (IOException expected) { }
            if (spool.pending().size() != 2) throw new AssertionError("pressure discarded a durable capture");
            for (var pending : spool.pending()) spool.acknowledge(pending);
        }
        var quotaConfig = JsonParser.parseString(Files.readString(quotaDescriptor)).getAsJsonObject();
        Path spoolRoot = Path.of(quotaConfig.get("spool").getAsString());
        Path retired = spoolRoot.resolve(".retired-" + quotaSource.boot());
        Files.move(spoolRoot.resolve(quotaSource.boot()), retired, StandardCopyOption.ATOMIC_MOVE);
        quotaConfig.addProperty("boot", id()); quotaConfig.addProperty("token", id() + id());
        Files.writeString(quotaDescriptor, quotaConfig.toString());
        var nextBoot = LocalSource.load(quotaDescriptor);
        try (var spool = new CaptureSpool(quotaDescriptor, nextBoot)) {
            if (Files.exists(retired) || !spool.pending().isEmpty())
                throw new AssertionError("interrupted acknowledged-boot cleanup did not recover");
        }
        Path byteDescriptor = Path.of(args[2]);
        var byteSource = LocalSource.load(byteDescriptor);
        try (var spool = new CaptureSpool(byteDescriptor, byteSource, 16, 1024)) {
            int accepted = 0;
            for (int index = 0; index < 8; index++) {
                try { spool.append(id(), "player.chat", now, chat("x".repeat(256))); accepted++; }
                catch (IOException full) { break; }
            }
            if (accepted == 0 || accepted >= 8 || spool.pending().size() != accepted)
                throw new AssertionError("byte pressure is not bounded and recoverable");
        }
        System.out.println("bridge durable spool checks passed");
    }
}
