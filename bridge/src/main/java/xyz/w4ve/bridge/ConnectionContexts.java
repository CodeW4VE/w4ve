package xyz.w4ve.bridge;

import java.util.IdentityHashMap;
import java.util.Map;
import java.util.UUID;

/** One context per live player object, shared with optional platform consumers.
 * Object arguments keep this small API independent of Minecraft remapping.
 */
public final class ConnectionContexts {
    private static final Map<Object, String> LIVE = new IdentityHashMap<>();
    private ConnectionContexts() {}
    public static synchronized String joined(Object player) {
        if (player == null) return "";
        String context = LIVE.get(player);
        if (context != null) return context;
        if (LIVE.size() >= 1000) return "";
        context = UUID.randomUUID().toString().replace("-", "");
        LIVE.put(player, context);
        return context;
    }
    public static synchronized String current(Object player) { return LIVE.getOrDefault(player, ""); }
    /** Respawn replaces ServerPlayer while retaining its network connection. */
    public static synchronized String respawn(Object previous, Object replacement) {
        if (previous == null || replacement == null) return "";
        String existing = LIVE.get(replacement);
        if (existing != null) return existing;
        String context = LIVE.remove(previous);
        if (context == null) return "";
        LIVE.put(replacement, context);
        return context;
    }
    public static synchronized String left(Object player) {
        String context = LIVE.remove(player);
        return context == null ? "" : context;
    }
    public static synchronized void clear() { LIVE.clear(); }
}
