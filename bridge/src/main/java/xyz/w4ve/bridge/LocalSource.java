package xyz.w4ve.bridge;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.charset.CodingErrorAction;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

/** Private local boot capability; never a player/instance/operator credential. */
public final class LocalSource {
    private final Path descriptor;
    private final String boot, token;
    private final boolean chat;
    private LocalSource(Path descriptor, String boot, String token, boolean chat) {
        this.descriptor = descriptor; this.boot = boot; this.token = token; this.chat = chat;
    }
    public String boot() { return boot; }
    public boolean chatEnabled() { return chat; }
    @Override public String toString() { return "LocalSource[boot=" + boot + "]"; }
    public static LocalSource load(Path path) throws IOException {
        return load(path, path);
    }
    public static LocalSource load(Path capability, Path endpoint) throws IOException {
        JsonObject value = descriptor(capability);
        String boot = value.get("boot").getAsString(), token = value.get("token").getAsString();
        if (!boot.matches("[0-9a-f]{32}") || !token.matches("[A-Za-z0-9_-]{16,128}"))
            throw new IOException("invalid private source capability");
        if (!value.has("chat") || !value.get("chat").isJsonPrimitive() || !value.getAsJsonPrimitive("chat").isBoolean())
            throw new IOException("invalid private chat policy");
        return new LocalSource(endpoint, boot, token, value.get("chat").getAsBoolean());
    }
    private static JsonObject descriptor(Path path) throws IOException {
        if (!path.isAbsolute() || Files.isSymbolicLink(path) || !Files.isRegularFile(path) || Files.size(path) > 8192)
            throw new IOException("invalid private source descriptor");
        try {
            for (var permission : Files.getPosixFilePermissions(path))
                if (permission == PosixFilePermission.GROUP_READ || permission == PosixFilePermission.GROUP_WRITE
                        || permission == PosixFilePermission.OTHERS_READ || permission == PosixFilePermission.OTHERS_WRITE)
                    throw new IOException("source descriptor permissions are too broad");
        } catch (UnsupportedOperationException windows) { /* Linux guardian owns production permissions. */ }
        try {
            var value = JsonParser.parseString(Files.readString(path, StandardCharsets.UTF_8)).getAsJsonObject();
            if (!"1".equals(value.get("schema").toString())
                    || !"w4ve-game-source".equals(value.get("owner").getAsString()))
                throw new IOException("invalid private source protocol");
            for (String key : new String[]{"boot", "token", "api_address", "api_socket"})
                if (!value.has(key) || !value.get(key).isJsonPrimitive() || !value.getAsJsonPrimitive(key).isString())
                    throw new IOException("invalid private source fields");
            return value;
        } catch (RuntimeException invalid) { throw new IOException("invalid private source descriptor"); }
    }
    public JsonObject status() throws IOException { return call("status", new JsonObject()); }
    public JsonObject append(JsonObject event) throws IOException { return call("append", event); }

    public static final class Refused extends IOException {
        public final int status;
        Refused(int status) { super("local source request refused (" + status + ")"); this.status = status; }
    }

    private JsonObject call(String action, JsonObject body) throws IOException {
        var config = descriptor(descriptor);
        // Read the endpoint again on every attempt: a guardian replacement may
        // move a loopback port without changing the game's boot capability.
        String address = config.get("api_address").getAsString();
        byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
        if (payload.length > 16384) throw new IOException("local source payload exceeds limit");
        String headers = "POST /v1/game/source/" + action + " HTTP/1.1\r\nHost: localhost\r\n"
                + "Authorization: Bearer " + token + "\r\nContent-Type: application/json\r\n"
                + "Content-Length: " + payload.length + "\r\nConnection: close\r\n\r\n";
        byte[] prefix = headers.getBytes(StandardCharsets.US_ASCII);
        ByteBuffer request = ByteBuffer.allocate(prefix.length + payload.length).put(prefix).put(payload);
        request.flip();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
        try (SocketChannel channel = address.isEmpty() ? SocketChannel.open(StandardProtocolFamily.UNIX) : SocketChannel.open();
             Selector selector = Selector.open()) {
            channel.configureBlocking(false);
            var target = address.isEmpty() ? UnixDomainSocketAddress.of(config.get("api_socket").getAsString())
                    : loopback(address);
            channel.connect(target);
            var key = channel.register(selector, SelectionKey.OP_CONNECT);
            while (!channel.finishConnect()) waitFor(selector, key, SelectionKey.OP_CONNECT, deadline);
            while (request.hasRemaining()) {
                checkDeadline(deadline);
                if (channel.write(request) == 0) waitFor(selector, key, SelectionKey.OP_WRITE, deadline);
            }
            ByteBuffer response = ByteBuffer.allocate(8192 + 65536);
            int end = -1, length = -1, code = 0;
            while (true) {
                checkDeadline(deadline);
                int count = channel.read(response);
                if (count < 0) {
                    if (end >= 0 && length >= 0 && response.position() == end + length)
                        return decode(response.array(), end, length, code);
                    throw new IOException("local source response ended early");
                }
                if (count == 0) { waitFor(selector, key, SelectionKey.OP_READ, deadline); continue; }
                if (end < 0) {
                    byte[] bytes = response.array();
                    for (int i = 3; i < response.position(); i++)
                        if (bytes[i-3] == 13 && bytes[i-2] == 10 && bytes[i-1] == 13 && bytes[i] == 10) { end = i+1; break; }
                    if (end < 0 && response.position() > 8192) throw new IOException("local source headers exceed limit");
                    if (end >= 0) {
                        if (end > 8192) throw new IOException("local source headers exceed limit");
                        String[] lines = new String(bytes, 0, end, StandardCharsets.US_ASCII).split("\r\n");
                        if (!lines[0].matches("HTTP/1\\.[01] [0-9]{3} .{0,128}")) throw new IOException("invalid local HTTP status");
                        code = Integer.parseInt(lines[0].substring(9,12));
                        for (int i = 1; i < lines.length; i++) {
                            String line = lines[i].toLowerCase(Locale.ROOT);
                            if (line.startsWith("transfer-encoding:")) throw new IOException("unsupported local HTTP encoding");
                            if (line.startsWith("content-length:")) {
                                String raw = line.substring(15).trim();
                                if (length != -1 || !raw.matches("[0-9]{1,5}")) throw new IOException("invalid local HTTP length");
                                length = Integer.parseInt(raw);
                            }
                        }
                        if (length < 0 || length > 65536) throw new IOException("local source response exceeds limit");
                    }
                }
                if (end >= 0 && response.position() >= end + length) {
                    if (response.position() != end + length) throw new IOException("trailing local response bytes");
                    // This protocol explicitly requests Connection: close and
                    // its server honors it. Confirm EOF to reject trailing data
                    // even when TCP splits it into another packet.
                }
                if (!response.hasRemaining()) throw new IOException("local source response exceeds limit");
            }
        }
    }
    private static JsonObject decode(byte[] bytes, int offset, int length, int code) throws IOException {
        if (code != 200) throw new Refused(code);
        try {
            String json = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes, offset, length)).toString();
            var result = JsonParser.parseString(json).getAsJsonObject();
            if (!result.has("ok") || !result.get("ok").isJsonPrimitive()
                    || !result.getAsJsonPrimitive("ok").isBoolean() || !result.get("ok").getAsBoolean())
                throw new IOException("local source operation failed");
            return result;
        } catch (RuntimeException invalid) { throw new IOException("invalid local source response"); }
    }
    private static java.net.SocketAddress loopback(String address) throws IOException {
        if (!address.matches("127\\.0\\.0\\.1:[0-9]{1,5}")) throw new IOException("source endpoint must be loopback");
        int port = Integer.parseInt(address.substring(address.indexOf(':') + 1));
        if (port < 1 || port > 65535) throw new IOException("invalid source port");
        return new InetSocketAddress("127.0.0.1", port);
    }
    private static void waitFor(Selector selector, SelectionKey key, int operation, long deadline) throws IOException {
        long remaining = deadline - System.nanoTime();
        if (remaining <= 0) throw new IOException("local source deadline exceeded");
        key.interestOps(operation);
        selector.select(Math.max(1, TimeUnit.NANOSECONDS.toMillis(remaining)));
        selector.selectedKeys().clear();
    }
    private static void checkDeadline(long deadline) throws IOException {
        if (System.nanoTime() >= deadline) throw new IOException("local source deadline exceeded");
    }
}
