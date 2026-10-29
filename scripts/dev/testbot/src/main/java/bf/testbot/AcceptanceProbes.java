package bf.testbot;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.player.PlayerModelPart;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.item.ItemStack;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;

/** Probes report client observations; server dumps and logs decide acceptance. */
public final class AcceptanceProbes {
    private static final List<Observation> OBSERVATIONS = new ArrayList<>();
    private static Nudge nudge;

    private AcceptanceProbes() {}

    static int mask(String value) {
        int mask =
                value.startsWith("0x") || value.startsWith("0X")
                        ? Integer.parseInt(value.substring(2), 16)
                        : Integer.parseInt(value);
        if (mask < 0 || mask > 0x7f)
            throw new IllegalArgumentException("skinparts mask 0x00..0x7F");
        return mask;
    }

    static double[] offset(String[] args) {
        if (args.length != 4) throw new IllegalArgumentException("nudge <dx> <dy> <dz>");
        double[] offset = new double[3];
        for (int i = 0; i < 3; i++) {
            offset[i] = Double.parseDouble(args[i + 1]);
            if (!Double.isFinite(offset[i]))
                throw new IllegalArgumentException("finite offset required");
        }
        return offset;
    }

    static boolean hitboxes(String[] args, boolean current) {
        if (args.length != 2) throw new IllegalArgumentException("hitboxes on|off|state");
        return switch (args[1]) {
            case "on" -> true;
            case "off" -> false;
            case "state" -> current;
            default -> throw new IllegalArgumentException("hitboxes on|off|state");
        };
    }

    static Object command(Minecraft mc, String[] args) {
        if (args[0].equals("hitboxes")) {
            var dispatcher = mc.getEntityRenderDispatcher();
            boolean enabled = hitboxes(args, dispatcher.shouldRenderHitBoxes());
            dispatcher.setRenderHitBoxes(enabled);
            return Map.of("hitboxes", dispatcher.shouldRenderHitBoxes());
        }
        if (mc.player == null || mc.getConnection() == null)
            throw new IllegalStateException("not connected");
        return switch (args[0]) {
            case "skinparts" -> {
                if (args.length > 2) throw new IllegalArgumentException("skinparts [mask]");
                if (args.length == 2) {
                    int value = mask(args[1]);
                    for (var part : PlayerModelPart.values())
                        mc.options.toggleModelPart(part, (value & part.getMask()) != 0);
                    mc.options.save();
                }
                int value = 0;
                for (var part : PlayerModelPart.values())
                    if (mc.options.isModelPartEnabled(part)) value |= part.getMask();
                yield Map.of("mask", value, "sentClientInformation", args.length == 2);
            }
            case "nudge" -> {
                double[] delta = offset(args);
                if (nudge != null) throw new IllegalStateException("nudge already pending");
                var p = mc.player;
                var before = position(mc);
                var target = p.position().add(delta[0], delta[1], delta[2]);
                if (!Double.isFinite(target.x)
                        || !Double.isFinite(target.y)
                        || !Double.isFinite(target.z))
                    throw new IllegalArgumentException("finite target required");
                p.setPos(target);
                nudge = new Nudge(before, position(mc));
                // Send the displaced position immediately; do not wait for vanilla's movement
                // threshold.
                p.connection.send(
                        new ServerboundMovePlayerPacket.Pos(
                                target.x, target.y, target.z, p.onGround()));
                yield observe(
                        mc,
                        () -> {
                            var result = nudge.result(position(mc));
                            nudge = null;
                            return result;
                        });
            }
            case "uniform" -> uniform(mc, args);
            default -> throw new IllegalArgumentException("unknown acceptance probe");
        };
    }

    private static List<Double> position(Minecraft mc) {
        var pos = mc.player.position();
        return List.of(pos.x, pos.y, pos.z);
    }

    public static void positionApplied(Minecraft mc) {
        if (nudge != null && mc.player != null) nudge.corrected(position(mc));
    }

    static final class Nudge {
        final List<Double> before;
        final List<Double> immediate;
        List<Double> reset;
        int corrections;

        Nudge(List<Double> before, List<Double> immediate) {
            this.before = List.copyOf(before);
            this.immediate = List.copyOf(immediate);
        }

        void corrected(List<Double> position) {
            corrections++;
            if (reset == null) reset = List.copyOf(position);
        }

        Map<String, Object> result(List<Double> current) {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("before", before);
            result.put("immediatelyAfter", immediate);
            result.put("afterServerReset", reset);
            result.put("afterObservation", List.copyOf(current));
            result.put("correctionsObserved", corrections);
            result.put(
                    "outcome",
                    reset == null ? "inconclusive:no_position_packet" : "position_packet_observed");
            return result;
        }
    }

    static final class Observation {
        final Object connection;
        final Supplier<Map<String, Object>> snapshot;
        final CompletableFuture<Object> future = new CompletableFuture<>();
        final long deadline;
        int ticks = 20;

        Observation(Object connection, Supplier<Map<String, Object>> snapshot, long now) {
            this.connection = connection;
            this.snapshot = snapshot;
            deadline = now + 3_000_000_000L;
        }

        boolean poll(Object currentConnection, long now) {
            if (future.isDone()) return true;
            if (connection != currentConnection) {
                future.completeExceptionally(
                        new IllegalStateException("connection changed during probe"));
                return true;
            }
            if (--ticks > 0 && now < deadline) return false;
            try {
                Map<String, Object> result = new LinkedHashMap<>(snapshot.get());
                result.put("observationTicks", 20 - ticks);
                result.put("deadlineReached", now >= deadline);
                future.complete(result);
            } catch (RuntimeException e) {
                future.completeExceptionally(e);
            }
            return true;
        }
    }

    private static CompletableFuture<Object> observe(
            Minecraft mc, Supplier<Map<String, Object>> snapshot) {
        var observation = new Observation(mc.getConnection(), snapshot, System.nanoTime());
        OBSERVATIONS.add(observation);
        return observation.future;
    }

    static void tick(Minecraft mc) {
        OBSERVATIONS.removeIf(
                observation -> observation.poll(mc.getConnection(), System.nanoTime()));
        if (OBSERVATIONS.isEmpty()) nudge = null;
    }

    static void clear() {
        for (var observation : OBSERVATIONS)
            observation.future.completeExceptionally(
                    new IllegalStateException("probe disconnected"));
        OBSERVATIONS.clear();
        nudge = null;
    }

    private static final String API = "io.wispforest.accessories.api.";
    private static final String NETWORK = "io.wispforest.accessories.networking.";

    private static Object uniform(Minecraft mc, String[] args) {
        if (args.length != 2)
            throw new IllegalArgumentException("uniform state|open|cosmetics|toggle|take|insert");
        try {
            Object container = uniformContainer(mc);
            if (args[1].equals("state")) return uniformState(mc, container);
            var before = uniformState(mc, container);
            Map<String, Object> sent = new LinkedHashMap<>();
            switch (args[1]) {
                case "open" -> {
                    Object variant =
                            Class.forName("io.wispforest.accessories.menu.AccessoriesMenuVariant")
                                    .getField("ORIGINAL")
                                    .get(null);
                    sendAccessories(
                            TestBot.construct(NETWORK + "server.ScreenOpen", -1, false, variant));
                    sent.put("packet", "ScreenOpen");
                    sent.put("entityId", -1);
                    sent.put("targetLookEntity", false);
                    sent.put("variant", "ORIGINAL");
                }
                case "cosmetics" -> {
                    Object property =
                            Class.forName(NETWORK + "holder.HolderProperty")
                                    .getField("COSMETIC_PROP")
                                    .get(null);
                    sendAccessories(
                            TestBot.construct(NETWORK + "holder.SyncHolderChange", property, true));
                    sent.put("packet", "SyncHolderChange");
                    sent.put("property", "COSMETIC_PROP");
                    sent.put("data", true);
                }
                case "toggle" -> {
                    // The pinned ToggleButton passes null for self, not the local player's entity
                    // id.
                    sendAccessories(
                            TestBot.construct(
                                    NETWORK + "server.SyncCosmeticToggle", null, "uniform", 0));
                    sent.put("packet", "SyncCosmeticToggle");
                    sent.put("entityId", null);
                    sent.put("slotName", "uniform");
                    sent.put("slotIndex", 0);
                }
                case "take", "insert" -> {
                    if (mc.gameMode == null) throw new IllegalStateException("no game mode");
                    Class<?> menuClass =
                            Class.forName(
                                    "io.wispforest.accessories.menu.variants.AccessoriesMenuBase");
                    var menu = mc.player.containerMenu;
                    if (!menuClass.isInstance(menu)
                            || !localMenu(
                                    menuClass.getMethod("owner").invoke(menu),
                                    menuClass.getMethod("targetEntity").invoke(menu),
                                    mc.player))
                        throw new IllegalStateException(
                                "open local Accessories menu with uniform open first");
                    boolean cosmetic = args[1].equals("insert");
                    if (cosmetic)
                        requireCosmeticsOpen(
                                (boolean)
                                        menu.getClass().getMethod("isCosmeticsOpen").invoke(menu));
                    var carried = menu.getCarried();
                    int slotId = uniformSlot(mc, container, cosmetic);
                    var slot = menu.slots.get(slotId);
                    validateClick(cosmetic, carried.isEmpty(), slot.getItem().isEmpty());
                    sent.put("packet", "ServerboundContainerClickPacket");
                    sent.put("containerId", menu.containerId);
                    sent.put("stateId", menu.getStateId());
                    sent.put("slot", slotId);
                    sent.put("slotName", "uniform");
                    sent.put("slotIndex", 0);
                    sent.put("cosmetic", cosmetic);
                    sent.put("button", 0);
                    sent.put("clickType", "PICKUP");
                    // Even when client prediction refuses the move, vanilla still sends the click.
                    mc.gameMode.handleInventoryMouseClick(
                            menu.containerId, slotId, 0, ClickType.PICKUP, mc.player);
                    sent.put("stateId", menu.getStateId());
                    sent.put("carried", stack(menu.getCarried()));
                }
                default ->
                        throw new IllegalArgumentException(
                                "uniform state|open|cosmetics|toggle|take|insert");
            }
            var immediate = uniformState(mc, container);
            return observe(
                    mc,
                    () -> {
                        try {
                            return Map.of(
                                    "sent",
                                    sent,
                                    "before",
                                    before,
                                    "immediatelyAfter",
                                    immediate,
                                    "afterObservation",
                                    uniformState(mc, uniformContainer(mc)));
                        } catch (ReflectiveOperationException e) {
                            throw new IllegalStateException("Accessories schema changed", e);
                        }
                    });
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("pinned Accessories API unavailable or changed", e);
        }
    }

    static boolean localMenu(Object owner, Object target, Object player) {
        return owner == player && (target == null || target == player);
    }

    static void requireCosmeticsOpen(boolean open) {
        if (!open)
            throw new IllegalStateException("open cosmetic panel with uniform cosmetics first");
    }

    static void validateClick(boolean cosmetic, boolean emptyCursor, boolean emptySlot) {
        if (cosmetic && emptyCursor)
            throw new IllegalStateException("pick up a cosmetic item with slotclick first");
        if (!cosmetic && !emptyCursor)
            throw new IllegalStateException("take requires an empty cursor");
        if (cosmetic && !emptySlot)
            throw new IllegalStateException("cosmetic uniform slot must be empty");
        if (!cosmetic && emptySlot) throw new IllegalStateException("uniform slot is empty");
    }

    private static void sendAccessories(Object packet) throws ReflectiveOperationException {
        Class.forName(NETWORK + "AccessoriesNetworking")
                .getMethod("sendToServer", Record.class)
                .invoke(null, packet);
    }

    private static Object uniformContainer(Minecraft mc) throws ReflectiveOperationException {
        var capabilityClass = Class.forName(API + "AccessoriesCapability");
        Object capability =
                capabilityClass.getMethod("get", LivingEntity.class).invoke(null, mc.player);
        if (capability == null) throw new IllegalStateException("no Accessories capability");
        var containers = (Map<?, ?>) capabilityClass.getMethod("getContainers").invoke(capability);
        Object container = containers.get("uniform");
        if (container == null) throw new IllegalStateException("no uniform container");
        if ((int) Class.forName(API + "AccessoriesContainer").getMethod("getSize").invoke(container)
                < 1) throw new IllegalStateException("no uniform slot");
        return container;
    }

    private static int uniformSlot(Minecraft mc, Object container, boolean cosmetic)
            throws ReflectiveOperationException {
        var type = Class.forName("io.wispforest.accessories.menu.AccessoriesInternalSlot");
        for (int i = 0; i < mc.player.containerMenu.slots.size(); i++) {
            var slot = mc.player.containerMenu.slots.get(i);
            if (type.isInstance(slot)
                    && slot.getContainerSlot() == 0
                    && type.getMethod("getContainer").invoke(slot) == container
                    && (boolean) type.getMethod("isCosmeticSlot").invoke(slot) == cosmetic)
                return i;
        }
        throw new IllegalStateException("uniform slot not present in the local Accessories menu");
    }

    private static Map<String, Object> uniformState(Minecraft mc, Object container)
            throws ReflectiveOperationException {
        var type = Class.forName(API + "AccessoriesContainer");
        var real =
                (net.minecraft.world.Container) type.getMethod("getAccessories").invoke(container);
        var cosmetic =
                (net.minecraft.world.Container)
                        type.getMethod("getCosmeticAccessories").invoke(container);
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("slotName", "uniform");
        state.put("slotIndex", 0);
        state.put("render", type.getMethod("shouldRender", int.class).invoke(container, 0));
        state.put("equipped", stack(real.getItem(0)));
        state.put("cosmetic", stack(cosmetic.getItem(0)));
        state.put("containerId", mc.player.containerMenu.containerId);
        state.put("carried", stack(mc.player.containerMenu.getCarried()));
        List<Object> slots = new ArrayList<>();
        var slotType = Class.forName("io.wispforest.accessories.menu.AccessoriesInternalSlot");
        for (int i = 0; i < mc.player.containerMenu.slots.size(); i++) {
            var slot = mc.player.containerMenu.slots.get(i);
            if (slotType.isInstance(slot)
                    && slot.getContainerSlot() == 0
                    && slotType.getMethod("getContainer").invoke(slot) == container)
                slots.add(
                        Map.of(
                                "slot",
                                i,
                                "cosmetic",
                                slotType.getMethod("isCosmeticSlot").invoke(slot),
                                "stack",
                                stack(slot.getItem()),
                                "mayPickup",
                                slot.mayPickup(mc.player),
                                "mayPlaceCarried",
                                slot.mayPlace(mc.player.containerMenu.getCarried()),
                                "active",
                                slot.isActive()));
        }
        var originalMenu = Class.forName("io.wispforest.accessories.menu.variants.AccessoriesMenu");
        state.put(
                "cosmeticsOpen",
                originalMenu.isInstance(mc.player.containerMenu)
                        ? originalMenu.getMethod("isCosmeticsOpen").invoke(mc.player.containerMenu)
                        : null);
        state.put("menuSlots", slots);
        return state;
    }

    private static Map<String, Object> stack(ItemStack stack) {
        return Map.of(
                "item",
                BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),
                "count",
                stack.getCount(),
                "components",
                stack.getComponents().toString());
    }
}
