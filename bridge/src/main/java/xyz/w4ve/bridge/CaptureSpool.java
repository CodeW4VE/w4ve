package xyz.w4ve.bridge;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/** Durable boundary: fsynced typed captures, bounded independently of the API. */
public final class CaptureSpool implements AutoCloseable {
    public record Pending(Path path, JsonObject event, LocalSource source) {
        public long sequence() { return event.get("sequence").getAsLong(); }
        @Override public String toString() { return "Pending[boot=" + source.boot() + ", sequence=" + sequence() + "]"; }
    }
    public static final class Expired extends IOException {
        Expired() { super("a capture expired; source reconciliation is required"); }
    }
    private final Path root, descriptor, directory;
    private final String boot;
    private final long retention;
    private final int maxEvents;
    private final long maxBytes;
    private final FileChannel lockChannel;
    private final FileLock lock;
    private final Path ownerKey;
    private static final Set<Path> OPEN_SPOOLS = ConcurrentHashMap.newKeySet();
    private long sequence;
    private boolean closed;
    public static final int MAX_EVENTS = 4096;
    public static final long MAX_BYTES = 8 * 1024 * 1024;

    public CaptureSpool(Path descriptor, LocalSource source) throws IOException {
        this(descriptor, source, MAX_EVENTS, MAX_BYTES);
    }
    public CaptureSpool(Path descriptor, LocalSource source, int maxEvents, long maxBytes) throws IOException {
        if (maxEvents < 1 || maxEvents > MAX_EVENTS || maxBytes < 1024 || maxBytes > MAX_BYTES)
            throw new IOException("invalid capture spool quotas");
        this.maxEvents = maxEvents; this.maxBytes = maxBytes;
        this.descriptor = descriptor;
        var config = read(descriptor, 8192);
        if (!source.boot().equals(config.get("boot").getAsString())) throw new IOException("capture boot changed");
        root = Path.of(config.get("spool").getAsString()).toAbsolutePath().normalize();
        Path expected = descriptor.getParent().getParent().resolve("game/capture-spool").toAbsolutePath().normalize();
        if (!root.equals(expected) || Files.isSymbolicLink(root) || !Files.isDirectory(root))
            throw new IOException("invalid managed capture spool directory");
        retention = config.get("retention").getAsLong();
        if (retention < 5 || retention > 3600) throw new IOException("invalid capture retention");
        boot = source.boot();
        directory = root.resolve(boot);
        var claim = claim(root);
        ownerKey = claim.key(); lockChannel = claim.channel(); lock = claim.lock();
        try {
            if (Files.isSymbolicLink(directory)) throw new IOException("invalid capture boot directory");
            if (!Files.exists(directory)) {
                if (bootDirectories().size() >= 64) throw new IOException("capture boot quota reached");
                privateDirectory(directory);
                atomic(directory.resolve("source.json"), Files.readAllBytes(descriptor));
            }
            recoverStaging(directory);
            if (!Files.exists(directory.resolve("source.json")))
                atomic(directory.resolve("source.json"), Files.readAllBytes(descriptor));
            LocalSource stored = LocalSource.load(directory.resolve("source.json"), descriptor);
            if (!stored.boot().equals(boot)) throw new IOException("capture descriptor belongs to another boot");
            Path counter = directory.resolve("sequence.json");
            sequence = 0;
            if (Files.exists(counter)) {
                var state = read(counter, 1024);
                if (!"1".equals(state.get("schema").toString()) || !boot.equals(state.get("boot").getAsString()))
                    throw new IOException("invalid capture sequence state");
                sequence = integer(state, "sequence");
            }
            for (var event : pending()) if (event.source().boot().equals(boot)) sequence = Math.max(sequence, event.sequence());
            saveSequence();
            pruneAcknowledgedBoots();
        } catch (IOException | RuntimeException invalid) {
            releaseClaim();
            if (invalid instanceof IOException io) throw io;
            throw new IOException("invalid capture spool state");
        }
    }

    public synchronized Pending append(String id, String kind, long occurred, JsonObject data) throws IOException {
        checkOpen();
        if (Files.exists(directory.resolve("fault.json"))) throw new IOException("capture source requires reconciliation");
        if (!id.matches("[0-9a-f]{32}") || occurred < 0 || sequence >= 9007199254740991L)
            throw new IOException("invalid capture identity");
        var event = new JsonObject();
        event.addProperty("schema", 1); event.addProperty("boot", boot); event.addProperty("id", id);
        event.addProperty("sequence", sequence + 1); event.addProperty("kind", kind);
        event.addProperty("occurred", occurred); event.add("data", data.deepCopy());
        byte[] raw = bytes(event);
        if (raw.length > 16384) throw new IOException("capture exceeds event size limit");
        var envelope = new JsonObject();
        envelope.addProperty("schema", 1); envelope.addProperty("owner", "w4ve-capture-event");
        envelope.addProperty("sha256", hash(raw)); envelope.add("event", event);
        byte[] body = bytes(envelope);
        List<Pending> captures = pending();
        long used = 0;
        for (var capture : captures) used += Files.size(capture.path());
        if (captures.size() >= maxEvents || used + body.length > maxBytes)
            throw new IOException("capture spool is full; source reconciliation is required");
        Path path = directory.resolve(String.format(java.util.Locale.ROOT, "%016d-%s.event.json", sequence + 1, id));
        if (Files.exists(path)) throw new IOException("capture identity already exists");
        atomic(path, body);
        sequence++;
        saveSequence(); // Before sending/deleting the durable event.
        return new Pending(path, event, LocalSource.load(directory.resolve("source.json"), descriptor));
    }

    public synchronized List<Pending> pending() throws IOException {
        checkOpen();
        var result = new ArrayList<Pending>();
        long used = 0;
        for (Path folder : bootDirectories()) {
            recoverStaging(folder);
            if (!Files.exists(folder.resolve("source.json"))) {
                boolean empty;
                try (var entries = Files.list(folder)) {
                    empty = entries.findAny().isEmpty();
                }
                if (empty) { Files.delete(folder); syncDirectory(root); continue; }
            }
            LocalSource source = LocalSource.load(folder.resolve("source.json"), descriptor);
            if (!folder.getFileName().toString().equals(source.boot())) throw new IOException("capture boot directory mismatch");
            try (var files = Files.list(folder)) {
                for (Path path : files.sorted().toList()) {
                    String name = path.getFileName().toString();
                    if (List.of("source.json", "sequence.json", "fault.json").contains(name)) continue;
                    if (!name.matches("[0-9]{16}-[0-9a-f]{32}\\.event\\.json") || Files.isSymbolicLink(path))
                        throw new IOException("unexpected file in managed capture spool; preserved");
                    used += Files.size(path);
                    if (result.size() >= maxEvents || used > maxBytes) throw new IOException("capture spool exceeds quota");
                    var envelope = read(path, 20000);
                    var event = envelope.getAsJsonObject("event");
                    if (!"1".equals(envelope.get("schema").toString()) || !"w4ve-capture-event".equals(envelope.get("owner").getAsString())
                            || !hash(bytes(event)).equals(envelope.get("sha256").getAsString())
                            || !source.boot().equals(event.get("boot").getAsString())
                            || !name.equals(String.format(java.util.Locale.ROOT, "%016d-%s.event.json", integer(event,"sequence"), event.get("id").getAsString())))
                        throw new IOException("capture record failed integrity checks");
                    result.add(new Pending(path, event, source));
                }
            }
        }
        // Sequence order is authoritative within a boot even if the wall clock
        // moves. Group by boot first; each stream has its own monotonically
        // increasing sequence and receiver checks it again.
        result.sort(Comparator.comparing((Pending value) -> value.source().boot()).thenComparingLong(Pending::sequence));
        for (int index = 1; index < result.size(); index++)
            if (result.get(index-1).source().boot().equals(result.get(index).source().boot())
                    && result.get(index-1).sequence() == result.get(index).sequence())
                throw new IOException("capture sequence has conflicting records");
        return List.copyOf(result);
    }

    public synchronized void acknowledge(Pending pending) throws IOException {
        checkOpen();
        Path path = pending.path().toAbsolutePath().normalize();
        if (!path.startsWith(root) || !path.getParent().getFileName().toString().equals(pending.source().boot())
                || Files.isSymbolicLink(path)) throw new IOException("invalid capture receipt target");
        if (!Files.exists(path)) return;
        var stored = read(path, 20000);
        if (!hash(bytes(pending.event())).equals(stored.get("sha256").getAsString()))
            throw new IOException("capture changed before acknowledgement");
        Files.delete(path);
        syncDirectory(path.getParent());
        pruneAcknowledgedBoots();
    }

    public synchronized void expire(long now) throws IOException {
        checkOpen();
        for (Pending pending : pending()) {
            if (pending.event().get("occurred").getAsLong() + retention <= now) {
                var fault = new JsonObject();
                fault.addProperty("schema", 1); fault.addProperty("owner", "w4ve-capture-fault");
                fault.addProperty("reason", "expired"); fault.addProperty("sequence", pending.sequence());
                atomic(pending.path().getParent().resolve("fault.json"), bytes(fault));
                Files.delete(pending.path());
                syncDirectory(pending.path().getParent());
            }
        }
        if (Files.exists(directory.resolve("fault.json"))) throw new Expired();
    }

    private List<Path> bootDirectories() throws IOException {
        var result = new ArrayList<Path>();
        try (var files = Files.list(root)) {
            for (Path path : files.toList()) {
                if (path.getFileName().toString().equals(".capture.lock")) continue;
                if (path.getFileName().toString().matches("\\.retired-[0-9a-f]{32}")) {
                    removeRetiredDirectory(path);
                    continue;
                }
                if (!path.getFileName().toString().matches("[0-9a-f]{32}") || Files.isSymbolicLink(path) || !Files.isDirectory(path))
                    throw new IOException("unexpected managed capture directory; preserved");
                if (result.size() >= 64) throw new IOException("capture boot quota reached");
                result.add(path);
            }
        }
        return result;
    }
    private void pruneAcknowledgedBoots() throws IOException {
        for (Path folder : bootDirectories()) {
            if (folder.equals(directory)) continue;
            boolean empty;
            try (var entries = Files.list(folder)) {
                empty = entries.allMatch(path -> List.of("source.json", "sequence.json").contains(path.getFileName().toString()));
            }
            if (!empty) continue;
            Path retired = root.resolve(".retired-" + folder.getFileName());
            Files.move(folder, retired, StandardCopyOption.ATOMIC_MOVE);
            syncDirectory(root);
            removeRetiredDirectory(retired);
        }
    }
    private void removeRetiredDirectory(Path folder) throws IOException {
        if (!folder.getParent().equals(root) || Files.isSymbolicLink(folder)) throw new IOException("invalid retired capture directory");
        try (var entries = Files.list(folder)) {
            for (Path path : entries.toList()) {
                if (!List.of("source.json", "sequence.json").contains(path.getFileName().toString()) || Files.isSymbolicLink(path))
                    throw new IOException("unexpected retired capture content; preserved");
            }
        }
        Files.deleteIfExists(folder.resolve("source.json"));
        Files.deleteIfExists(folder.resolve("sequence.json"));
        Files.delete(folder);
        syncDirectory(root);
    }
    private void saveSequence() throws IOException {
        var state = new JsonObject();
        state.addProperty("schema", 1); state.addProperty("boot", boot); state.addProperty("sequence", sequence);
        atomic(directory.resolve("sequence.json"), bytes(state));
    }
    private static void recoverStaging(Path folder) throws IOException {
        List<Path> staged;
        try (var entries = Files.list(folder)) {
            staged = entries.filter(path -> path.getFileName().toString().startsWith(".capture-")).toList();
        }
        if (staged.size() > MAX_EVENTS) throw new IOException("capture staging exceeds quota");
        for (Path temporary : staged) {
            String name = temporary.getFileName().toString();
            if (!name.matches("\\.capture-(?:source\\.json|sequence\\.json|fault\\.json|[0-9]{16}-[0-9a-f]{32}\\.event\\.json)-[0-9]+\\.tmp")
                    || Files.isSymbolicLink(temporary)) throw new IOException("unknown staged capture file; preserved");
            String stem = name.substring(".capture-".length(), name.length()-4);
            String targetName = stem.substring(0, stem.lastIndexOf('-'));
            Path target = folder.resolve(targetName);
            if (Files.exists(target)) {
                if (targetName.endsWith(".event.json") && !read(target,20000).equals(read(temporary,20000)))
                    throw new IOException("staged capture conflicts with a committed record");
                Files.delete(temporary);
                syncDirectory(folder);
                continue;
            }
            try {
                var value = read(temporary, 20000);
                if (targetName.equals("source.json")) {
                    if (!LocalSource.load(temporary).boot().equals(folder.getFileName().toString()))
                        throw new IOException("staged source belongs to a different boot");
                } else if (targetName.equals("sequence.json")) {
                    if (!folder.getFileName().toString().equals(value.get("boot").getAsString()))
                        throw new IOException("staged sequence belongs to a different boot");
                    integer(value,"sequence");
                } else if (targetName.equals("fault.json")) {
                    if (!"w4ve-capture-fault".equals(value.get("owner").getAsString()))
                        throw new IOException("invalid staged capture fault");
                } else {
                    var event = value.getAsJsonObject("event");
                    String expected = String.format(java.util.Locale.ROOT, "%016d-%s.event.json", integer(event,"sequence"), event.get("id").getAsString());
                    if (!"w4ve-capture-event".equals(value.get("owner").getAsString()) || !expected.equals(targetName)
                            || !folder.getFileName().toString().equals(event.get("boot").getAsString())
                            || !hash(bytes(event)).equals(value.get("sha256").getAsString()))
                        throw new IOException("invalid staged capture integrity");
                }
                Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE);
                syncDirectory(folder);
            } catch (RuntimeException invalid) {
                throw new IOException("incomplete capture staging requires reconciliation");
            }
        }
    }
    private static long integer(JsonObject value, String key) throws IOException {
        if (!value.has(key) || !value.get(key).toString().matches("[0-9]{1,16}")) throw new IOException("invalid capture integer");
        long number = value.get(key).getAsLong();
        if (number > 9007199254740991L) throw new IOException("capture integer exceeds limit");
        return number;
    }
    private static JsonObject read(Path path, int maximum) throws IOException {
        if (Files.isSymbolicLink(path) || !Files.isRegularFile(path) || Files.size(path) > maximum)
            throw new IOException("invalid private capture file");
        try { return JsonParser.parseString(Files.readString(path, StandardCharsets.UTF_8)).getAsJsonObject(); }
        catch (RuntimeException invalid) { throw new IOException("invalid private capture data"); }
    }
    private static byte[] bytes(JsonObject value) { return value.toString().getBytes(StandardCharsets.UTF_8); }
    private static String hash(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (java.security.NoSuchAlgorithmException impossible) { throw new AssertionError("SHA-256 unavailable"); }
    }
    private static void privateDirectory(Path path) throws IOException {
        try { Files.createDirectory(path, PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rwx------"))); }
        catch (UnsupportedOperationException windows) { Files.createDirectory(path); }
        syncDirectory(path.getParent());
    }
    private static void atomic(Path path, byte[] bytes) throws IOException {
        if (Files.isSymbolicLink(path)) throw new IOException("private capture target is a symlink");
        Path temporary;
        try { temporary = Files.createTempFile(path.getParent(), ".capture-" + path.getFileName() + "-", ".tmp",
                    PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------"))); }
        catch (UnsupportedOperationException windows) { temporary = Files.createTempFile(path.getParent(), ".capture-" + path.getFileName() + "-", ".tmp"); }
        try {
            try (FileChannel file = FileChannel.open(temporary, StandardOpenOption.WRITE)) {
                ByteBuffer buffer = ByteBuffer.wrap(bytes);
                while (buffer.hasRemaining()) file.write(buffer);
                file.force(true);
            }
            Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            syncDirectory(path.getParent());
        } finally { Files.deleteIfExists(temporary); }
    }
    private static void syncDirectory(Path path) throws IOException {
        if (System.getProperty("os.name").startsWith("Windows")) return;
        try (FileChannel directory = FileChannel.open(path, StandardOpenOption.READ)) { directory.force(true); }
    }
    private void checkOpen() throws IOException { if (closed) throw new IOException("capture spool is closed"); }
    private record Claim(Path key, FileChannel channel, FileLock lock) {}
    private static Claim claim(Path root) throws IOException {
        Path key = root.toRealPath();
        // On POSIX, closing another channel for this inode can release a JVM's
        // process-wide lock. Reject same-JVM contenders before opening it.
        if (!OPEN_SPOOLS.add(key)) throw new IOException("capture spool is already in use");
        FileChannel channel = null;
        try {
            channel = FileChannel.open(root.resolve(".capture.lock"), StandardOpenOption.CREATE, StandardOpenOption.WRITE);
            FileLock acquired = channel.tryLock();
            if (acquired == null) throw new IOException("capture spool is already in use");
            return new Claim(key, channel, acquired);
        } catch (IOException | RuntimeException failed) {
            if (channel != null) channel.close();
            OPEN_SPOOLS.remove(key);
            throw new IOException("capture spool is already in use");
        }
    }
    private void releaseClaim() throws IOException {
        try { lock.release(); }
        finally { try { lockChannel.close(); } finally { OPEN_SPOOLS.remove(ownerKey); } }
    }
    @Override public synchronized void close() throws IOException {
        if (!closed) { closed = true; releaseClaim(); }
    }
}
