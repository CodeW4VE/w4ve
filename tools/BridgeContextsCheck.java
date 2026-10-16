import xyz.w4ve.bridge.ConnectionContexts;
import xyz.w4ve.wavemotes.server.PlatformConnections;

/** Shared object identity; deliberately not an in-game player test. */
public final class BridgeContextsCheck {
    public static void main(String[] args) {
        if (args.length > 0) {
            Object player = new Object();
            String joined = PlatformConnections.joined(player), current = PlatformConnections.current(player);
            String respawn = PlatformConnections.respawn(player, new Object());
            if (args[0].equals("--standalone")) {
                if (joined != null || current != null || respawn != null) throw new AssertionError("absent platform blocked standalone mode");
            } else if (joined == null || current == null || respawn == null || !joined.isEmpty() || !current.isEmpty() || !respawn.isEmpty()) {
                throw new AssertionError("broken platform did not fail closed");
            }
            System.out.println("optional context API: passed " + args[0]);
            return;
        }
        Object first = new String("same-player"), reconnected = new String("same-player");
        ConnectionContexts.clear();
        String context = PlatformConnections.joined(first);
        if (context == null || !context.matches("[0-9a-f]{32}")
                || !context.equals(ConnectionContexts.joined(first)))
            throw new AssertionError("join callbacks disagree on shared context");
        String replacement = ConnectionContexts.joined(reconnected);
        if (replacement.equals(context) || !replacement.equals(PlatformConnections.joined(reconnected)))
            throw new AssertionError("reconnection reused an old context");
        ConnectionContexts.left(first);
        if (!PlatformConnections.current(first).isEmpty() || !replacement.equals(PlatformConnections.current(reconnected)))
            throw new AssertionError("late disconnect removed replacement connection");
        Object respawned = new Object();
        if (!replacement.equals(PlatformConnections.respawn(reconnected, respawned))
                || !replacement.equals(ConnectionContexts.respawn(reconnected, respawned))
                || !PlatformConnections.current(reconnected).isEmpty())
            throw new AssertionError("respawn did not transfer shared context idempotently");
        ConnectionContexts.left(reconnected);
        if (!replacement.equals(PlatformConnections.current(respawned)))
            throw new AssertionError("old player object invalidated respawn context");
        ConnectionContexts.clear();
        for (int index = 0; index < 1000; index++)
            if (ConnectionContexts.joined(new Object()).isEmpty()) throw new AssertionError("premature roster pressure");
        if (!PlatformConnections.joined(new Object()).isEmpty()) throw new AssertionError("roster quota bypassed");
        ConnectionContexts.clear();
        if (!PlatformConnections.current(reconnected).isEmpty()) throw new AssertionError("shutdown retained connection");
        System.out.println("shared connection identity, reconnect, respawn, callback order, quota and shutdown: passed");
    }
}
