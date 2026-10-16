package xyz.w4ve.bridge;

import com.google.gson.JsonObject;
import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.fabricmc.fabric.api.entity.event.v1.ServerPlayerEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.network.chat.ChatType;
import net.minecraft.server.level.ServerPlayer;
import java.security.MessageDigest;
import java.util.HexFormat;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Typed public game capture with a bounded durable spool, independent of WaveChat. */
public final class W4VEBridge implements ModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("w4ve-bridge");
    private final ArrayBlockingQueue<JsonObject> queue = new ArrayBlockingQueue<>(256);
    private final AtomicInteger pending = new AtomicInteger();
    private volatile boolean running;
    private volatile boolean failed;
    private LocalSource source;
    private Thread worker;

    @Override public void onInitialize() {
        ServerLifecycleEvents.SERVER_STARTING.register(server -> start());
        ServerLifecycleEvents.SERVER_STARTED.register(server -> capture("server.started"));
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> capture("server.stopping"));
        ServerLifecycleEvents.SERVER_STOPPED.register(server -> close());
        ServerPlayerEvents.AFTER_RESPAWN.register((previous, replacement, alive) -> {
            if (ConnectionContexts.respawn(previous, replacement).isEmpty() && running) fault();
        });
        ServerPlayConnectionEvents.JOIN.register((handler, sender, server) -> {
            String connection = ConnectionContexts.joined(handler.player);
            if (connection.isEmpty()) { fault(); return; }
            var data = player(handler.player, connection);
            data.addProperty("name", handler.player.getGameProfile().getName());
            capture("player.joined", data);
        });
        ServerPlayConnectionEvents.DISCONNECT.register((handler, server) -> {
            String connection = ConnectionContexts.left(handler.player);
            if (!connection.isEmpty()) capture("player.left", player(handler.player, connection));
        });
        // CHAT_MESSAGE is after ALLOW_CHAT_MESSAGE. Public commands also arrive
        // here: accept only the ordinary chat registry type, never /say or /me.
        ServerMessageEvents.CHAT_MESSAGE.register((message, sender, params) -> {
            if (!running || failed || !source.chatEnabled() || !params.chatType().is(ChatType.CHAT)
                    || message.isSystem() || !message.sender().equals(sender.getUUID())
                    || !message.filterMask().isEmpty()) return;
            String connection = ConnectionContexts.current(sender);
            if (connection.isEmpty()) { fault(); return; }
            String text = message.decoratedContent().getString();
            if (text.isEmpty() || text.codePointCount(0, text.length()) > 256
                    || text.codePoints().anyMatch(value -> value < 32 || value == 127)) { fault(); return; }
            var data = player(sender, connection);
            data.addProperty("text", text);
            String signature = "";
            if (message.signature() != null) {
                try { signature = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(message.signature().bytes())); }
                catch (java.security.NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
            }
            data.addProperty("signature_sha256", signature);
            capture("player.chat", data);
        });
    }
    private static JsonObject player(ServerPlayer player, String connection) {
        var data = new JsonObject();
        data.addProperty("uuid", player.getUUID().toString().replace("-", ""));
        data.addProperty("connection", connection);
        return data;
    }
    private void fault() {
        if (!failed) LOG.warn("W4VE game capture lost continuity; source requires reconciliation");
        failed = true;
    }
    private void start() {
        ConnectionContexts.clear();
        String file = System.getenv("W4VE_GAME_SOURCE_CONFIG");
        if (file == null || file.isBlank()) {
            LOG.info("W4VE game source is not configured; bridge remains inactive");
            return;
        }
        try {
            source = LocalSource.load(Path.of(file));
            running = true;
            failed = false;
            worker = new Thread(this::work, "w4ve-bridge-capture");
            worker.setDaemon(true);
            worker.start();
        } catch (Exception invalid) {
            // Config exceptions can contain private input. Log only a fixed diagnosis.
            LOG.warn("W4VE private source descriptor is unavailable; bridge is inactive");
        }
    }
    private void capture(String kind) {
        capture(kind, new JsonObject());
    }
    private void capture(String kind, JsonObject data) {
        if (!running || failed) return;
        var event = new JsonObject();
        event.addProperty("schema", 1); event.addProperty("boot", source.boot());
        event.addProperty("id", UUID.randomUUID().toString().replace("-", ""));
        event.addProperty("kind", kind); event.addProperty("occurred", System.currentTimeMillis()/1000);
        event.add("data", data);
        pending.incrementAndGet();
        if (!queue.offer(event)) {
            pending.decrementAndGet();
            failed = true;
            LOG.warn("W4VE game capture queue is full; source requires reconciliation");
        }
    }
    private void work() {
        String file = System.getenv("W4VE_GAME_SOURCE_CONFIG");
        try (var spool = new CaptureSpool(Path.of(file), source)) {
            boolean connected = false;
            long retryAt = 0;
            while (running || pending.get() != 0) {
                var event = queue.poll(200, TimeUnit.MILLISECONDS);
                if (event != null) {
                    spool.append(event.get("id").getAsString(), event.get("kind").getAsString(),
                            event.get("occurred").getAsLong(), event.getAsJsonObject("data"));
                    pending.decrementAndGet();
                }
                if (System.nanoTime() >= retryAt) {
                    try {
                        var state = source.status();
                        if (!source.boot().equals(state.get("boot").getAsString()) || state.get("retired").getAsBoolean())
                            throw new LocalSource.Refused(409);
                        flush(spool);
                        if (!connected) LOG.info("W4VE local game source connected (durable capture adapter)");
                        connected = true;
                        retryAt = System.nanoTime() + TimeUnit.SECONDS.toNanos(1);
                    } catch (LocalSource.Refused refused) {
                        if (refused.status != 429 && refused.status != 503) throw refused;
                        retryAt = System.nanoTime() + TimeUnit.SECONDS.toNanos(1);
                    } catch (java.io.IOException unavailable) {
                        if (unavailable instanceof CaptureSpool.Expired) throw unavailable;
                        retryAt = System.nanoTime() + TimeUnit.SECONDS.toNanos(1);
                    }
                }
            }
            try { flush(spool); }
            catch (java.io.IOException unavailable) { LOG.warn("W4VE game captures remain in the private spool for recovery"); }
        } catch (Exception unavailable) {
            failed = true;
            LOG.warn("W4VE game source is unavailable; capture requires reconciliation");
        }
    }
    private static void flush(CaptureSpool spool) throws java.io.IOException {
        spool.expire(System.currentTimeMillis()/1000);
        for (var capture : spool.pending()) {
            var result = capture.source().append(capture.event());
            if (!result.has("sequence") || !result.get("sequence").toString().matches("[0-9]{1,16}")
                    || result.get("sequence").getAsLong() != capture.sequence())
                throw new java.io.IOException("source sequence mismatch");
            spool.acknowledge(capture);
        }
    }
    private void close() {
        running = false;
        if (worker != null) {
            try { worker.join(5000); }
            catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        }
        if (pending.get() > 0) LOG.warn("W4VE game source stopped before reaching its durable capture boundary");
        ConnectionContexts.clear();
    }
}
