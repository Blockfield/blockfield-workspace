package bf.testbot;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicInteger;

public final class AcceptanceProbesTest {
    private AcceptanceProbesTest() {}

    public record ToggleFixture(Integer entityId, String slotName, int slotIndex) {}

    public record HolderFixture(Object property, Object data) {}

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static void rejects(Runnable action) {
        try {
            action.run();
        } catch (IllegalArgumentException | IllegalStateException expected) {
            return;
        }
        throw new AssertionError("invalid probe accepted");
    }

    public static void main(String[] args) throws ReflectiveOperationException {
        check(AcceptanceProbes.mask("0x00") == 0, "all parts off");
        check(AcceptanceProbes.mask("0X7F") == 127, "all parts on");
        check(AcceptanceProbes.mask("126") == 126, "decimal mask");
        check(AcceptanceProbes.mask("010") == 10, "leading zero remains decimal");
        for (String invalid : List.of("-1", "128", "0x80", "NaN", "0x", ""))
            rejects(() -> AcceptanceProbes.mask(invalid));
        check(
                AcceptanceProbes.hitboxes(new String[] {"hitboxes", "on"}, false),
                "enable debug boxes");
        check(
                !AcceptanceProbes.hitboxes(new String[] {"hitboxes", "off"}, true),
                "disable debug boxes");
        for (boolean enabled : List.of(true, false))
            check(
                    AcceptanceProbes.hitboxes(new String[] {"hitboxes", "state"}, enabled)
                            == enabled,
                    "read debug boxes");
        rejects(() -> AcceptanceProbes.hitboxes(new String[] {"hitboxes"}, true));
        rejects(() -> AcceptanceProbes.hitboxes(new String[] {"hitboxes", "toggle"}, true));
        rejects(() -> AcceptanceProbes.hitboxes(new String[] {"hitboxes", "on", "off"}, true));
        double[] offset = AcceptanceProbes.offset(new String[] {"nudge", "100", "-0.25", "1e2"});
        check(offset[0] == 100 && offset[1] == -0.25 && offset[2] == 100, "signed displacement");
        for (String invalid : List.of("NaN", "Infinity", "-Infinity", "1e309", "text"))
            rejects(() -> AcceptanceProbes.offset(new String[] {"nudge", "0", invalid, "0"}));
        rejects(() -> AcceptanceProbes.offset(new String[] {"nudge", "1", "2"}));
        rejects(() -> AcceptanceProbes.offset(new String[] {"nudge", "1", "2", "3", "4"}));

        List<Double> original = new ArrayList<>(List.of(0d, 64d, 0d));
        var nudge = new AcceptanceProbes.Nudge(original, List.of(100d, 64d, 0d));
        original.set(0, 99d);
        var noReset = nudge.result(List.of(100d, 64d, 0d));
        check(
                noReset.get("afterServerReset") == null
                        && noReset.get("correctionsObserved").equals(0),
                "no packet is inconclusive");
        check(
                noReset.get("outcome").equals("inconclusive:no_position_packet"),
                "no correction verdict invented");
        List<Double> reset = new ArrayList<>(List.of(0d, 64d, 0d));
        nudge.corrected(reset);
        reset.set(0, 99d);
        nudge.corrected(List.of(0.1d, 64d, 0d));
        var result = nudge.result(List.of(0.2d, 64d, 0d));
        check(result.get("before").equals(List.of(0d, 64d, 0d)), "immutable before position");
        check(
                result.get("afterServerReset").equals(List.of(0d, 64d, 0d)),
                "first applied reset retained");
        check(
                result.get("afterObservation").equals(List.of(0.2d, 64d, 0d))
                        && result.get("correctionsObserved").equals(2),
                "count every packet, retain final position");
        check(
                new AcceptanceProbes.Nudge(original, original).corrections == 0,
                "new probe resets count");

        Object connection = new Object();
        AtomicInteger snapshots = new AtomicInteger();
        var observation =
                new AcceptanceProbes.Observation(
                        connection, () -> Map.of("sample", snapshots.incrementAndGet()), 0);
        for (int i = 0; i < 19; i++)
            check(!observation.poll(connection, 1), "wait for observation ticks");
        check(
                observation.poll(connection, 2) && snapshots.get() == 1,
                "snapshot on twentieth tick");
        check(
                ((Map<?, ?>) observation.future.join()).get("observationTicks").equals(20),
                "ticks reported");
        check(observation.poll(connection, 3) && snapshots.get() == 1, "completion is one-shot");
        var deadline = new AcceptanceProbes.Observation(connection, () -> Map.of("sample", 1), 100);
        check(deadline.poll(connection, 3_000_000_100L), "wall-clock deadline bounds slow ticks");
        check(
                ((Map<?, ?>) deadline.future.join()).get("deadlineReached").equals(true),
                "deadline reported");
        var disconnected =
                new AcceptanceProbes.Observation(
                        connection,
                        () -> {
                            throw new AssertionError("snapshot after disconnect");
                        },
                        0);
        check(disconnected.poll(new Object(), 1), "connection replacement ends probe");
        try {
            disconnected.future.join();
            throw new AssertionError("disconnect accepted as evidence");
        } catch (CompletionException expected) {
            check(expected.getCause() instanceof IllegalStateException, "disconnect is explicit");
        }
        var cancelled =
                new AcceptanceProbes.Observation(
                        connection,
                        () -> {
                            throw new AssertionError("cancelled snapshot");
                        },
                        0);
        cancelled.future.cancel(false);
        check(cancelled.poll(connection, 1), "cancelled observation removed");
        var failed =
                new AcceptanceProbes.Observation(
                        connection,
                        () -> {
                            throw new IllegalStateException("fixture failure");
                        },
                        0);
        check(
                failed.poll(connection, 3_000_000_000L) && failed.future.isCompletedExceptionally(),
                "snapshot error preserved");

        check(
                AcceptanceProbes.localMenu(connection, null, connection),
                "pinned self-menu uses null target");
        check(
                AcceptanceProbes.localMenu(connection, connection, connection),
                "explicit self target");
        check(
                !AcceptanceProbes.localMenu(connection, new Object(), connection),
                "remote target rejected");
        check(!AcceptanceProbes.localMenu(new Object(), null, connection), "other owner rejected");
        AcceptanceProbes.requireCosmeticsOpen(true);
        rejects(() -> AcceptanceProbes.requireCosmeticsOpen(false));
        AcceptanceProbes.validateClick(false, true, false);
        AcceptanceProbes.validateClick(true, false, true);
        rejects(() -> AcceptanceProbes.validateClick(false, false, false));
        rejects(() -> AcceptanceProbes.validateClick(false, true, true));
        rejects(() -> AcceptanceProbes.validateClick(true, true, true));
        rejects(() -> AcceptanceProbes.validateClick(true, false, false));
        check(
                TestBot.GSON.toJson(noReset).contains("\"afterServerReset\":null"),
                "TCP response preserves unavailable reset");
        var packet =
                (ToggleFixture)
                        TestBot.construct(ToggleFixture.class.getName(), null, "uniform", 0);
        check(
                packet.entityId() == null
                        && packet.slotName().equals("uniform")
                        && packet.slotIndex() == 0,
                "pinned self-toggle constructor");
        check(
                TestBot.GSON.toJson(packet).contains("\"entityId\":null"),
                "self-toggle JSON retains null entity id");
        var holder =
                (HolderFixture)
                        TestBot.construct(HolderFixture.class.getName(), "COSMETIC_PROP", true);
        check(
                holder.property().equals("COSMETIC_PROP") && holder.data().equals(true),
                "cosmetic panel request is idempotent");
        System.out.println("Acceptance probe checks passed");
    }
}
