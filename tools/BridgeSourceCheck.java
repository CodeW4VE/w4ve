import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;
import xyz.w4ve.bridge.LocalSource;

/** Pure Java21 IPC harness, no Minecraft or graphical assertions. */
public final class BridgeSourceCheck {
    public static void main(String[] args) throws Exception {
        Path planPath = Path.of(args[0]);
        var plan = JsonParser.parseString(Files.readString(planPath)).getAsJsonObject();
        var source = LocalSource.load(Path.of(plan.get("positive").getAsString()));
        if (source.status().get("sequence").getAsLong() != 0) throw new AssertionError("fixture source is not fresh");
        var event = new JsonObject();
        event.addProperty("schema", 1); event.addProperty("boot", source.boot());
        event.addProperty("id", UUID.randomUUID().toString().replace("-", ""));
        event.addProperty("sequence", 1); event.addProperty("kind", "server.started");
        event.addProperty("occurred", System.currentTimeMillis()/1000);
        event.add("data", new JsonObject());
        long cursor = source.append(event).get("cursor").getAsLong();
        var repeated = source.append(event);
        if (!repeated.get("duplicate").getAsBoolean() || repeated.get("cursor").getAsLong() != cursor)
            throw new AssertionError("fixture replay did not preserve its receipt");
        event.addProperty("kind", "server.stopping");
        try { source.append(event); throw new AssertionError("changed replay was accepted"); }
        catch (LocalSource.Refused refused) { if (refused.status != 409) throw new AssertionError("wrong conflict status"); }
        for (var item : plan.getAsJsonArray("negative")) {
            var test = item.getAsJsonObject();
            var client = LocalSource.load(Path.of(test.get("descriptor").getAsString()));
            long start = System.nanoTime();
            try { client.status(); throw new AssertionError("unsafe local response accepted: " + test.get("name").getAsString()); }
            catch (IOException refused) {
                if (refused.getMessage() != null && refused.getMessage().contains("PRIVATE_FIXTURE"))
                    throw new AssertionError("private response echoed in diagnosis");
            }
            if ((System.nanoTime() - start)/1e9 > 4.5) throw new AssertionError("local request exceeded its deadline");
        }
        System.out.println("bridge local source checks passed");
    }
}
