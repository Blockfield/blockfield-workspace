package bf.testbot;

import bf.testbot.mixin.KeyMappingAccessor;
import bf.testbot.mixin.KeyboardHandlerInvoker;
import bf.testbot.mixin.MinecraftInputAccess;
import bf.testbot.mixin.MouseHandlerInvoker;

import com.google.gson.Gson;
import com.mojang.blaze3d.platform.InputConstants;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraft.client.gui.screens.ConnectScreen;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.multiplayer.ServerData;
import net.minecraft.client.multiplayer.resolver.ServerAddress;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

import org.lwjgl.glfw.GLFW;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Управление headless-клиентом по TCP (127.0.0.1:47777, одна команда = одна строка, ответ = одна
 * строка JSON). Клавиши идут тем же путём, что и GLFW: KeyMapping.click + KeyMapping.set, поэтому
 * миксины на KeyMapping (наш spread, Keybind Fix Plus) проверяются по-честному.
 */
public final class TestBot implements ClientModInitializer {
    static final Gson GSON = new com.google.gson.GsonBuilder().serializeNulls().create();
    private static final Deque<String> MESSAGES = new ArrayDeque<>();
    private static final List<Map<String, Object>> SOUND_LOG = new ArrayList<>();
    private static boolean soundListener;
    private static final Map<InputConstants.Key, Integer> HELD_KEYS = new HashMap<>();
    private static final Map<KeyMapping, Integer> HELD_MAPPINGS = new HashMap<>();
    private static final RawInputState RAW_INPUT = new RawInputState();
    private static int tick;
    private static int frame;
    private static boolean renderProbe;
    private static final List<Map<String, Object>> RENDER_EVENTS = new ArrayList<>();
    private static final List<int[]> SLOT_SEQUENCE = new ArrayList<>();
    private static final List<String> WATCH = new ArrayList<>();
    private static volatile String trackName;
    private static ApprovedBlueprint approvedBlueprint;

    private record ApprovedBlueprint(UUID ownerId, UUID jobId, List<Integer> origin, String plan) {}

    private static net.minecraft.world.entity.Entity findTarget(Minecraft mc, String name) {
        for (net.minecraft.world.entity.Entity e : mc.level.entitiesForRendering()) {
            if (e.hasCustomName() && e.getCustomName().getString().equals(name)
                    || e instanceof net.minecraft.world.entity.player.Player
                            && e.getName().getString().equalsIgnoreCase(name)) {
                return e;
            }
        }
        return null;
    }

    /**
     * Points the camera at the target's chest where this frame renders it; returns the aim point.
     */
    private static net.minecraft.world.phys.Vec3 aimAt(
            Minecraft mc, LocalPlayer p, net.minecraft.world.entity.Entity target) {
        float partial = mc.getTimer().getGameTimeDeltaPartialTick(true);
        net.minecraft.world.phys.Vec3 renderPos = target.getPosition(partial);
        net.minecraft.world.phys.Vec3 aimPoint =
                new net.minecraft.world.phys.Vec3(
                        renderPos.x, renderPos.y + target.getEyeHeight() * 0.9, renderPos.z);
        net.minecraft.world.phys.Vec3 eye = p.getEyePosition(partial);
        double dx = aimPoint.x - eye.x, dy = aimPoint.y - eye.y, dz = aimPoint.z - eye.z;
        float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float pitch = (float) -Math.toDegrees(Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)));
        p.setYRot(yaw);
        p.setXRot(pitch);
        p.setYHeadRot(yaw);
        return aimPoint;
    }

    private static boolean watching;

    /**
     * Raw C2S payload for a channel this build's mod no longer knows (sbw_laser on bf20);
     * registered only then.
     */
    public record RawPayload(
            net.minecraft.network.protocol.common.custom.CustomPacketPayload.Type<RawPayload>
                    theType,
            byte[] data)
            implements net.minecraft.network.protocol.common.custom.CustomPacketPayload {
        @Override
        public net.minecraft.network.protocol.common.custom.CustomPacketPayload.Type<
                        ? extends net.minecraft.network.protocol.common.custom.CustomPacketPayload>
                type() {
            return theType;
        }
    }

    private static net.minecraft.network.protocol.common.custom.CustomPacketPayload.Type<RawPayload>
            LASER_RAW_TYPE;

    @Override
    public void onInitializeClient() {
        maybeRegisterRawLaser();
        ClientReceiveMessageEvents.GAME.register(
                (msg, overlay) -> remember((overlay ? "[ACTIONBAR] " : "") + msg.getString()));
        ClientReceiveMessageEvents.CHAT.register(
                (msg, signed, sender, params, ts) -> remember(msg.getString()));
        ClientPlayConnectionEvents.JOIN.register(
                (handler, sender, mc) -> {
                    approvedBlueprint = null;
                    AcceptanceProbes.clear();
                    RenderedPlayerGeometry.clear();
                });
        ClientPlayConnectionEvents.DISCONNECT.register(
                (handler, mc) -> {
                    approvedBlueprint = null;
                    AcceptanceProbes.clear();
                    RenderedPlayerGeometry.clear();
                    resetRawInput(mc);
                });
        ClientTickEvents.END_CLIENT_TICK.register(
                mc -> {
                    tick++;
                    AcceptanceProbes.tick(mc);
                    RenderedPlayerGeometry.expire(mc.level);
                    for (RawInputState.Key key : RAW_INPUT.expire(tick, System.nanoTime()))
                        releaseRawInput(mc, key);
                    SLOT_SEQUENCE.removeIf(
                            action -> {
                                if (tick < action[0]) return false;
                                if (mc.player != null)
                                    mc.player.getInventory().selected = action[1];
                                return true;
                            });
                    HELD_KEYS
                            .entrySet()
                            .removeIf(
                                    e -> {
                                        if (tick >= e.getValue()) {
                                            KeyMapping.set(e.getKey(), false);
                                            return true;
                                        }
                                        return false;
                                    });
                    HELD_MAPPINGS
                            .entrySet()
                            .removeIf(
                                    e -> {
                                        if (tick >= e.getValue()) {
                                            e.getKey().setDown(false);
                                            return true;
                                        }
                                        return false;
                                    });
                });
        // Sampled at tick start: the state the previous tick left for rendering, after every mod's
        // end-tick handler.
        ClientTickEvents.START_CLIENT_TICK.register(
                mc -> {
                    if (mc.player != null) sample(mc);
                });
        ClientTickEvents.START_CLIENT_TICK.register(Parkour::tick);
        // "track <name>": keep the crosshair on the target every tick, before this tick's movement
        // packet is sent,
        // like a player following an enemy with the mouse (shots then go through "tacz shoot").
        ClientTickEvents.START_CLIENT_TICK.register(
                mc -> {
                    if (trackName == null || mc.player == null || mc.level == null) return;
                    net.minecraft.world.entity.Entity target = findTarget(mc, trackName);
                    if (target != null) aimAt(mc, mc.player, target);
                });
        net.fabricmc.fabric.api.client.rendering.v1.WorldRenderEvents.LAST.register(
                ctx -> sampleFrame(ctx.tickCounter().getGameTimeDeltaPartialTick(true)));
        int port = Integer.getInteger("bf.testbot.port", 47777);
        Thread t = new Thread(() -> serve(port), "bf-testbot");
        t.setDaemon(true);
        t.start();
    }

    /**
     * SBW's LaserShootMessage was removed in bf20 (PR #11). When the class is gone the channel has
     * no codec, so register a raw C2S payload for it here (init-time only) — then `forge sbw_laser`
     * can put bytes on `superbwarfare:laser_shoot`. When the class exists (bf19) SBW registers the
     * real codec and `forge sbw_laser` constructs the message by reflection instead.
     */
    private static void maybeRegisterRawLaser() {
        try {
            Class.forName("com.atsuishio.superbwarfare.network.message.send.LaserShootMessage");
            return;
        } catch (ClassNotFoundException ignored) {
        }
        var id =
                net.minecraft.resources.ResourceLocation.fromNamespaceAndPath(
                        "superbwarfare", "laser_shoot");
        LASER_RAW_TYPE =
                new net.minecraft.network.protocol.common.custom.CustomPacketPayload.Type<>(id);
        net.minecraft.network.codec.StreamCodec<net.minecraft.network.FriendlyByteBuf, RawPayload>
                codec =
                        net.minecraft.network.codec.StreamCodec.of(
                                (buf, payload) -> buf.writeBytes(payload.data()),
                                buf -> {
                                    byte[] b = new byte[buf.readableBytes()];
                                    buf.readBytes(b);
                                    return new RawPayload(LASER_RAW_TYPE, b);
                                });
        net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry.playC2S()
                .register(LASER_RAW_TYPE, codec);
    }

    /**
     * Builds a mod's own payload object by reflection, choosing the constructor whose parameter
     * types the given args fit (null matches any object type, boxed args match primitives). Errors
     * name the class and every declared constructor so a schema change is obvious. MC types (Vec3)
     * come in as compiled objects so Loom remaps them; never Class.forName an MC class by its
     * Mojang name.
     */
    static Object construct(String className, Object... args) throws ReflectiveOperationException {
        Class<?> cls = Class.forName(className);
        for (java.lang.reflect.Constructor<?> c : cls.getDeclaredConstructors()) {
            Class<?>[] pt = c.getParameterTypes();
            if (pt.length != args.length) continue;
            boolean ok = true;
            for (int i = 0; i < pt.length; i++) {
                if (!ctorAssignable(pt[i], args[i])) {
                    ok = false;
                    break;
                }
            }
            if (ok) {
                c.setAccessible(true);
                return c.newInstance(args);
            }
        }
        StringBuilder sb = new StringBuilder();
        for (var c : cls.getDeclaredConstructors()) sb.append("\n  ").append(c);
        throw new IllegalStateException(
                "no constructor of " + className + " fits given args; available:" + sb);
    }

    private static boolean ctorAssignable(Class<?> param, Object arg) {
        if (arg == null) return !param.isPrimitive();
        if (param.isInstance(arg)) return true;
        Class<?> a = arg.getClass();
        return (param == int.class && a == Integer.class)
                || (param == long.class && a == Long.class)
                || (param == double.class && a == Double.class)
                || (param == float.class && a == Float.class)
                || (param == boolean.class && a == Boolean.class)
                || (param == short.class && a == Short.class)
                || (param == byte.class && a == Byte.class);
    }

    /**
     * watch sample: "<sprint key><sprinting><ParCool FastRun modifier> v<horizontal blocks/s> o<FOV
     * modifier>".
     */
    private static synchronized void sample(Minecraft mc) {
        if (!watching) return;
        LocalPlayer p = mc.player;
        var speed =
                p.getAttribute(net.minecraft.world.entity.ai.attributes.Attributes.MOVEMENT_SPEED);
        boolean fastRun =
                speed != null
                        && speed.getModifier(
                                        net.minecraft.resources.ResourceLocation
                                                .fromNamespaceAndPath(
                                                        "parcool", "modifier.speed.fastrun"))
                                != null;
        Vec3 v = p.getDeltaMovement();
        WATCH.add(
                (mc.options.keySprint.isDown() ? "1" : "0")
                        + (p.isSprinting() ? "1" : "0")
                        + (fastRun ? "1" : "0")
                        + " v"
                        + r(Math.hypot(v.x, v.z) * 20)
                        + " o"
                        + r(p.getFieldOfViewModifier())
                        + taczSynced(p));
        if (WATCH.size() >= 1200) watching = false;
    }

    /**
     * " r<TACZ synced reload state> a<synced aiming 0|1>", empty without TACZ; reflection keeps the
     * bot TACZ-free.
     */
    private static String taczSynced(LocalPlayer p) {
        try {
            Object op =
                    Class.forName("com.tacz.guns.api.entity.IGunOperator")
                            .getMethod(
                                    "fromLivingEntity",
                                    net.minecraft.world.entity.LivingEntity.class)
                            .invoke(null, p);
            Object reload = op.getClass().getMethod("getSynReloadState").invoke(op);
            Object type = reload.getClass().getMethod("getStateType").invoke(reload);
            boolean aiming = (boolean) op.getClass().getMethod("getSynIsAiming").invoke(op);
            return " r" + type + " a" + (aiming ? 1 : 0);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return "";
        }
    }

    private static synchronized Object watch(String mode) {
        if ("start".equals(mode)) {
            WATCH.clear();
            watching = true;
            return Map.of("ok", true);
        }
        watching = false;
        return Map.of("samples", new ArrayList<>(WATCH));
    }

    private static synchronized void remember(String line) {
        MESSAGES.addLast(line);
        while (MESSAGES.size() > 200) MESSAGES.removeFirst();
    }

    private static void serve(int port) {
        try (ServerSocket server = new ServerSocket(port, 5, InetAddress.getLoopbackAddress())) {
            while (true) {
                try (Socket s = server.accept();
                        BufferedReader in =
                                new BufferedReader(
                                        new InputStreamReader(
                                                s.getInputStream(), StandardCharsets.UTF_8));
                        PrintWriter out =
                                new PrintWriter(
                                        new OutputStreamWriter(
                                                s.getOutputStream(), StandardCharsets.UTF_8),
                                        true)) {
                    String line;
                    while ((line = in.readLine()) != null) {
                        String cmd = line.trim();
                        if (cmd.isEmpty()) continue;
                        Object result;
                        try {
                            result =
                                    Minecraft.getInstance()
                                            .submit(() -> exec(cmd))
                                            .get(30, TimeUnit.SECONDS);
                            if (result
                                    instanceof java.util.concurrent.CompletableFuture<?> pending) {
                                try {
                                    result = pending.get(5, TimeUnit.SECONDS);
                                } finally {
                                    pending.cancel(false);
                                }
                            }
                        } catch (Exception e) {
                            Throwable c = e.getCause() != null ? e.getCause() : e;
                            result = Map.of("error", c.toString());
                        }
                        out.println(GSON.toJson(result));
                    }
                } catch (Exception ignored) {
                }
            }
        } catch (Exception e) {
            System.err.println("[bftestbot] " + e);
        }
    }

    private static Object blockfieldScreen(Minecraft mc) {
        if (mc.screen == null) throw new IllegalStateException("no screen");
        if (!mc.screen.getClass().getSimpleName().equals("TabletScreen")) return mc.screen;
        try {
            Class<?> tablet = Class.forName("com.lunares.blockfield.client.gui.tablet.Tablet");
            var tab = tablet.getDeclaredField("tab");
            tab.setAccessible(true);
            var field =
                    tablet.getDeclaredField(
                            "DEPLOY".equals(String.valueOf(tab.get(null))) ? "deployment" : "menu");
            field.setAccessible(true);
            Object menu = field.get(null);
            if (menu == null) throw new IllegalStateException("tablet menu is loading");
            return menu;
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("tablet schema changed", e);
        }
    }

    private static Object exec(String line) {
        Minecraft mc = Minecraft.getInstance();
        String[] a = line.split("\\s+");
        String cmd = a[0];
        LocalPlayer p = mc.player;
        switch (cmd) {
            case "state":
                return state(mc);
            case "modelaudit":
                return ModelAudit.audit(mc, a);
            case "modelpage":
                return ModelAudit.page(mc, a);
            case "creativeaudit":
                return ModelAudit.tabs(mc, a);
            case "melee":
                {
                    requirePlayer(p);
                    if (a.length != 2 || mc.gameMode == null || mc.level == null)
                        throw new IllegalArgumentException(
                                "melee requires one visible player name");
                    var target =
                            mc.level.players().stream()
                                    .filter(
                                            other ->
                                                    other != p
                                                            && other.getGameProfile()
                                                                    .getName()
                                                                    .equals(a[1]))
                                    .findFirst()
                                    .orElseThrow(
                                            () ->
                                                    new IllegalArgumentException(
                                                            "player not visible"));
                    if (!target.isAlive() || p.distanceToSqr(target) > 9)
                        throw new IllegalArgumentException(
                                "target not alive or outside three-block melee range");
                    mc.gameMode.attack(
                            p, target); // Normal client attack packet; no server/console shortcut.
                    p.swing(net.minecraft.world.InteractionHand.MAIN_HAND);
                    return Map.of("attacked", a[1]);
                }

            case "bfmenu":
                {
                    Object screen = blockfieldScreen(mc);
                    try {
                        var field =
                                screen.getClass()
                                        .getDeclaredField(
                                                switch (screen.getClass().getSimpleName()) {
                                                    case "ClassSelectionScreen" -> "classesData";
                                                    case "SpawnSelectionScreen" -> "spawnsData";
                                                    default -> "data";
                                                });
                        field.setAccessible(true);
                        return ((com.google.gson.JsonObject) field.get(screen)).deepCopy();
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("not a Blockfield menu", e);
                    }
                }
            case "uipreview":
                {
                    if (mc.getCurrentServer() == null
                            || !"127.0.0.1:25599".equals(mc.getCurrentServer().ip))
                        throw new IllegalStateException(
                                "UI fixtures require the local test server");
                    var fixture =
                            com.google.gson.JsonParser.parseString(
                                            new String(
                                                    java.util.Base64.getDecoder().decode(a[1]),
                                                    java.nio.charset.StandardCharsets.UTF_8))
                                    .getAsJsonObject();
                    String kind = fixture.get("kind").getAsString();
                    String data = fixture.get("data").toString();
                    try {
                        if (kind.equals("menu")) {
                            Class.forName("com.lunares.blockfield.client.gui.tablet.Tablet")
                                    .getMethod("openMenu", String.class, String.class)
                                    .invoke(null, data, "rig-fixture");
                        } else {
                            net.minecraft.client.gui.screens.Screen screen =
                                    switch (kind) {
                                        case "class" ->
                                                (net.minecraft.client.gui.screens.Screen)
                                                        Class.forName(
                                                                        "com.lunares.blockfield.client.gui.screen.ClassSelectionScreen")
                                                                .getConstructor(String.class)
                                                                .newInstance(data);
                                        case "spawn" ->
                                                (net.minecraft.client.gui.screens.Screen)
                                                        Class.forName(
                                                                        "com.lunares.blockfield.client.gui.screen.SpawnSelectionScreen")
                                                                .getConstructor(
                                                                        String.class, String.class)
                                                                .newInstance(data, "rig-fixture");
                                        default ->
                                                throw new IllegalArgumentException(
                                                        "fixture kind menu|class|spawn");
                                    };
                            mc.setScreen(screen);
                            if (fixture.has("ui"))
                                screen.getClass()
                                        .getMethod("updateModel", String.class)
                                        .invoke(screen, fixture.get("ui").toString());
                        }
                        return state(mc);
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("UI fixture schema changed", e);
                    }
                }
            case "bfui":
                {
                    Object screen = blockfieldScreen(mc);
                    try {
                        var method =
                                screen.getClass()
                                        .getDeclaredMethod("layout", double.class, double.class);
                        method.setAccessible(true);
                        Map<String, Object> result = new LinkedHashMap<>();
                        result.put("layout", method.invoke(screen, -1d, -1d));
                        result.put("screen", screen.getClass().getSimpleName());
                        result.put(
                                "gui",
                                List.of(
                                        mc.getWindow().getGuiScaledWidth(),
                                        mc.getWindow().getGuiScaledHeight()));
                        if (mc.screen != screen) {
                            Class<?> tablet =
                                    Class.forName(
                                            "com.lunares.blockfield.client.gui.tablet.Tablet");
                            var field =
                                    tablet.getDeclaredField(
                                            screen.getClass().getSimpleName().equals("MenuScreen")
                                                    ? "canvas"
                                                    : "deploymentCanvas");
                            field.setAccessible(true);
                            result.put("canvas", field.get(null));
                        }
                        return result;
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("unsupported Blockfield layout", e);
                    }
                }
            case "uiclick":
                {
                    if (mc.screen == null) throw new IllegalStateException("no screen");
                    double x = Double.parseDouble(a[1]), y = Double.parseDouble(a[2]);
                    boolean clicked = mc.screen.mouseClicked(x, y, 0);
                    if (mc.screen != null) mc.screen.mouseReleased(x, y, 0);
                    return Map.of("clicked", clicked);
                }
            case "uikey":
                {
                    if (mc.screen == null) throw new IllegalStateException("no screen");
                    return Map.of(
                            "handled",
                            mc.screen.keyPressed(
                                    Integer.parseInt(a[1]),
                                    0,
                                    a.length > 2 ? Integer.parseInt(a[2]) : 0));
                }
            case "uiscale":
                {
                    int scale = Integer.parseInt(a[1]);
                    if (scale < 1 || scale > 4) throw new IllegalArgumentException("uiscale 1..4");
                    mc.options.guiScale().set(scale);
                    mc.resizeDisplay();
                    return state(mc);
                }
            case "widgets":
                {
                    List<Map<String, Object>> widgets = new ArrayList<>();
                    if (mc.screen != null) {
                        for (int i = 0; i < mc.screen.children().size(); i++) {
                            var child = mc.screen.children().get(i);
                            if (child
                                    instanceof
                                    net.minecraft.client.gui.components.AbstractWidget w) {
                                widgets.add(
                                        Map.of(
                                                "index",
                                                i,
                                                "text",
                                                w.getMessage().getString(),
                                                "x",
                                                w.getX(),
                                                "y",
                                                w.getY(),
                                                "width",
                                                w.getWidth(),
                                                "height",
                                                w.getHeight(),
                                                "active",
                                                w.active,
                                                "visible",
                                                w.visible));
                            }
                        }
                    }
                    return widgets;
                }
            case "click":
                {
                    if (mc.screen == null) throw new IllegalStateException("no screen");
                    var child = mc.screen.children().get(Integer.parseInt(a[1]));
                    if (!(child instanceof net.minecraft.client.gui.components.AbstractWidget w))
                        throw new IllegalArgumentException("not a widget");
                    double x = w.getX() + w.getWidth() / 2.0, y = w.getY() + w.getHeight() / 2.0;
                    boolean clicked = mc.screen.mouseClicked(x, y, 0);
                    if (mc.screen != null) mc.screen.mouseReleased(x, y, 0);
                    return Map.of("clicked", clicked);
                }
            case "slots":
                {
                    // Слоты открытого контейнера (сундук, инвентарь) и предмет на курсоре.
                    requirePlayer(p);
                    var menu = p.containerMenu;
                    List<String> slots = new ArrayList<>();
                    for (int i = 0; i < menu.slots.size(); i++) {
                        var stack = menu.slots.get(i).getItem();
                        if (!stack.isEmpty())
                            slots.add(
                                    i
                                            + " "
                                            + stack.getCount()
                                            + "x"
                                            + net.minecraft.core.registries.BuiltInRegistries.ITEM
                                                    .getKey(stack.getItem()));
                    }
                    var carried = menu.getCarried();
                    return Map.of(
                            "container",
                            menu.containerId,
                            "size",
                            menu.slots.size(),
                            "slots",
                            slots,
                            "carried",
                            carried.isEmpty()
                                    ? ""
                                    : carried.getCount()
                                            + "x"
                                            + net.minecraft.core.registries.BuiltInRegistries.ITEM
                                                    .getKey(carried.getItem()));
                }
            case "slotclick":
                {
                    // slotclick <slot> [button] [PICKUP|QUICK_MOVE|SWAP|THROW|...]: обычный пакет
                    // клика клиента, slot -999 -- вне окна.
                    requirePlayer(p);
                    if (mc.gameMode == null) throw new IllegalStateException("no game mode");
                    var type =
                            net.minecraft.world.inventory.ClickType.valueOf(
                                    a.length > 3 ? a[3] : "PICKUP");
                    mc.gameMode.handleInventoryMouseClick(
                            p.containerMenu.containerId,
                            Integer.parseInt(a[1]),
                            a.length > 2 ? Integer.parseInt(a[2]) : 0,
                            type,
                            p);
                    return exec("slots");
                }
            case "camera":
                {
                    mc.options.setCameraType(net.minecraft.client.CameraType.valueOf(a[1]));
                    return Map.of("camera", mc.options.getCameraType().name());
                }
            case "keys":
                {
                    List<Map<String, Object>> keys = new ArrayList<>();
                    for (KeyMapping m : mc.options.keyMappings) {
                        Map<String, Object> k = new LinkedHashMap<>();
                        k.put("name", m.getName());
                        k.put("key", m.saveString());
                        k.put("down", m.isDown());
                        k.put("clicks", ((KeyMappingAccessor) m).bf$clickCount());
                        keys.add(k);
                    }
                    return keys;
                }
            case "press":
                {
                    InputConstants.Key key = InputConstants.getKey(a[1]);
                    int ticks = a.length > 2 ? Integer.parseInt(a[2]) : 2;
                    KeyMapping.click(key);
                    KeyMapping.set(key, true);
                    HELD_KEYS.put(key, tick + ticks);
                    return Map.of("pressed", a[1], "ticks", ticks);
                }
            case "use":
                return InputCommands.use(mc, a);
            case "use_on":
                return InputCommands.useOn(mc, a);
            case "sneak":
                return InputCommands.sneak(mc, a);
            case "scroll":
                return InputCommands.scroll(mc, a);
            case "attack":
                return exec("press key.mouse.left" + (a.length > 1 ? " " + a[1] : ""));
            case "hold":
                {
                    KeyMapping m = mappingOf(mc, a[1]);
                    int ticks = Integer.parseInt(a[2]);
                    m.setDown(true);
                    HELD_MAPPINGS.put(m, tick + ticks);
                    return Map.of("hold", m.getName(), "ticks", ticks);
                }
            case "look":
                {
                    requirePlayer(p);
                    p.setYRot(Float.parseFloat(a[1]));
                    p.setXRot(Float.parseFloat(a[2]));
                    p.setYHeadRot(p.getYRot());
                    return Map.of("yaw", p.getYRot(), "pitch", p.getXRot());
                }
            case "lookat":
                {
                    requirePlayer(p);
                    Vec3 eye = p.getEyePosition();
                    double dx = Double.parseDouble(a[1]) - eye.x,
                            dy = Double.parseDouble(a[2]) - eye.y,
                            dz = Double.parseDouble(a[3]) - eye.z;
                    float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
                    float pitch =
                            (float) -Math.toDegrees(Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)));
                    return exec("look " + yaw + " " + pitch);
                }
            case "block":
                {
                    if (a.length != 4) throw new IllegalArgumentException("block <x> <y> <z>");
                    if (mc.level == null) throw new IllegalStateException("not in a world");
                    BlockPos pos =
                            new BlockPos(
                                    Integer.parseInt(a[1]),
                                    Integer.parseInt(a[2]),
                                    Integer.parseInt(a[3]));
                    var block = mc.level.getBlockState(pos);
                    var collision = block.getCollisionShape(mc.level, pos);
                    return Map.of(
                            "pos",
                            pos.toShortString(),
                            "state",
                            block.toString(),
                            "outlineEmpty",
                            block.getShape(mc.level, pos).isEmpty(),
                            "collisionEmpty",
                            collision.isEmpty(),
                            "collisionBoxes",
                            collision.toAabbs().stream()
                                    .map(
                                            box ->
                                                    List.of(
                                                            box.minX, box.minY, box.minZ, box.maxX,
                                                            box.maxY, box.maxZ))
                                    .toList());
                }
            case "slot":
                {
                    requirePlayer(p);
                    p.getInventory().selected = Integer.parseInt(a[1]);
                    return Map.of("slot", p.getInventory().selected);
                }
            case "rawinput":
                return rawInput(mc, a);
            case "sequence":
                {
                    requirePlayer(p);
                    if (a.length < 2 || a.length > 41)
                        throw new IllegalArgumentException("sequence <tick:slot> ... (max 40)");
                    List<int[]> actions = new ArrayList<>();
                    for (int i = 1; i < a.length; i++) {
                        String[] pair = a[i].split(":", -1);
                        if (pair.length != 2) throw new IllegalArgumentException("tick:slot");
                        int offset = Integer.parseInt(pair[0]), slot = Integer.parseInt(pair[1]);
                        if (offset < 0 || offset > 200 || slot < 0 || slot > 8)
                            throw new IllegalArgumentException("tick 0..200, slot 0..8");
                        actions.add(new int[] {tick + offset, slot});
                    }
                    SLOT_SEQUENCE.clear();
                    for (int[] action : actions) {
                        if (action[0] == tick) p.getInventory().selected = action[1];
                        else SLOT_SEQUENCE.add(action);
                    }
                    return Map.of("startTick", tick, "actions", actions.size());
                }
            case "renderprobe":
                {
                    if (a.length != 2) throw new IllegalArgumentException("renderprobe start|get");
                    if (a[1].equals("start")) {
                        RENDER_EVENTS.clear();
                        renderProbe = true;
                        return Map.of("tick", tick, "frame", frame);
                    }
                    if (!a[1].equals("get"))
                        throw new IllegalArgumentException("renderprobe start|get");
                    renderProbe = false;
                    return new ArrayList<>(RENDER_EVENTS);
                }
            case "chat":
                {
                    requirePlayer(p);
                    String text = line.substring(cmd.length()).trim();
                    if (text.startsWith("/")) p.connection.sendCommand(text.substring(1));
                    else p.connection.sendChat(text);
                    return Map.of("sent", text);
                }
            case "messages":
                {
                    int n = a.length > 1 ? Integer.parseInt(a[1]) : 10;
                    synchronized (TestBot.class) {
                        List<String> all = new ArrayList<>(MESSAGES);
                        return all.subList(Math.max(0, all.size() - n), all.size());
                    }
                }
            case "clearmessages":
                {
                    synchronized (TestBot.class) {
                        MESSAGES.clear();
                    }
                    return Map.of("ok", true);
                }
            case "screenshot":
                {
                    String name = a[1].endsWith(".png") ? a[1] : a[1] + ".png";
                    Screenshot.grab(mc.gameDirectory, name, mc.getMainRenderTarget(), c -> {});
                    return Map.of(
                            "file",
                            mc.gameDirectory
                                    .toPath()
                                    .resolve("screenshots")
                                    .resolve(name)
                                    .toString());
                }
            case "closescreen":
                mc.setScreen(null);
                return Map.of("ok", true);
            case "screen":
                {
                    // screen inventory|options|video|workshopai -- открыть экран без мыши (video =
                    // Sodium,
                    // рефлексией)
                    net.minecraft.client.gui.screens.Screen s;
                    try {
                        s =
                                switch (a[1]) {
                                    case "inventory" ->
                                            new net.minecraft.client.gui.screens.inventory
                                                    .InventoryScreen(p);
                                    case "options" ->
                                            new net.minecraft.client.gui.screens.options
                                                    .OptionsScreen(mc.screen, mc.options);
                                    case "video" ->
                                            (net.minecraft.client.gui.screens.Screen)
                                                    Class.forName(
                                                                    "net.caffeinemc.mods.sodium.client.gui.VideoSettingsScreen")
                                                            .getMethod(
                                                                    "createScreen",
                                                                    net.minecraft.client.gui.screens
                                                                            .Screen.class)
                                                            .invoke(null, mc.screen);
                                    case "workshopai" ->
                                            (net.minecraft.client.gui.screens.Screen)
                                                    Class.forName(
                                                                    "com.lunares.blockfield.client.workshop.WorkshopAiScreen")
                                                            .getConstructor()
                                                            .newInstance();
                                    default ->
                                            throw new IllegalArgumentException(
                                                    "screen inventory|options|video|workshopai");
                                };
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("screen failed: " + e);
                    }
                    mc.setScreen(s);
                    return Map.of("screen", s.getClass().getSimpleName());
                }
            case "respawn":
                requirePlayer(p);
                p.respawn();
                return Map.of("ok", true);
            case "disconnect":
                {
                    if (mc.level != null) {
                        mc.level.disconnect();
                        mc.disconnect();
                    }
                    mc.setScreen(new TitleScreen());
                    return Map.of("ok", true);
                }
            case "connect":
                {
                    if (mc.level != null) {
                        mc.level.disconnect();
                        mc.disconnect();
                    }
                    ServerAddress addr = ServerAddress.parseString(a[1]);
                    ConnectScreen.startConnecting(
                            new TitleScreen(),
                            mc,
                            addr,
                            new ServerData("rig", a[1], ServerData.Type.OTHER),
                            false,
                            null);
                    return Map.of("connecting", a[1]);
                }
            case "bfaction":
                {
                    // Действие меню Blockfield как от кнопки экрана ("vote:konstaxa",
                    // "class:assault:0", "spawn:BASE"):
                    // рефлексией, чтобы бот не зависел от blockfield-client на этапе сборки.
                    Object msg;
                    try {
                        msg =
                                Class.forName(
                                                "com.lunares.blockfield.network.packet.MenuActionC2SPacket")
                                        .getConstructor(String.class)
                                        .newInstance(a[1]);
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("blockfield-client не загружен: " + e);
                    }
                    net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking.send(
                            (net.minecraft.network.protocol.common.custom.CustomPacketPayload) msg);
                    return Map.of("sent", a[1]);
                }
            case "tacz":
                {
                    // Оператор оружия TACZ клиента, минуя обработку мыши: tacz shoot|reload|aim
                    // <true|false>
                    // Результат shoot -- ShootResult (SUCCESS, IS_SPRINTING, NOT_DRAW, ...), т.е.
                    // видно, ПОЧЕМУ не стреляет.
                    requirePlayer(p);
                    Object op;
                    try {
                        op =
                                Class.forName(
                                                "com.tacz.guns.api.client.gameplay.IClientPlayerGunOperator")
                                        .getMethod("fromLocalPlayer", LocalPlayer.class)
                                        .invoke(null, p);
                        switch (a[1]) {
                            case "shoot":
                                {
                                    long wallTimeMs = System.currentTimeMillis();
                                    return Map.of(
                                            "result",
                                            String.valueOf(
                                                    op.getClass().getMethod("shoot").invoke(op)),
                                            "tick",
                                            tick,
                                            "nanoTime",
                                            System.nanoTime(),
                                            "wallTimeMs",
                                            wallTimeMs);
                                }
                            case "status":
                                {
                                    Object holder =
                                            op.getClass().getMethod("getDataHolder").invoke(op);
                                    Object synced =
                                            Class.forName("com.tacz.guns.api.entity.IGunOperator")
                                                    .getMethod(
                                                            "fromLivingEntity",
                                                            net.minecraft.world.entity.LivingEntity
                                                                    .class)
                                                    .invoke(null, p);
                                    return Map.of(
                                            "tick",
                                            tick,
                                            "wallTimeMs",
                                            System.currentTimeMillis(),
                                            "drawTimestamp",
                                            holder.getClass()
                                                    .getField("clientDrawTimestamp")
                                                    .get(holder),
                                            "stateLock",
                                            holder.getClass()
                                                    .getField("clientStateLock")
                                                    .get(holder),
                                            "drawCooldownMs",
                                            synced.getClass()
                                                    .getMethod("getSynDrawCoolDown")
                                                    .invoke(synced));
                                }
                            case "reload":
                                op.getClass().getMethod("reload").invoke(op);
                                return Map.of("ok", true);
                            case "aim":
                                op.getClass()
                                        .getMethod("aim", boolean.class)
                                        .invoke(op, Boolean.parseBoolean(a[2]));
                                return Map.of("ok", true);
                            // TACZ's own melee/inspect client paths, independent of whether a key
                            // is bound.
                            case "melee":
                                op.getClass().getMethod("melee").invoke(op);
                                return Map.of("ok", true);
                            case "inspect":
                                op.getClass().getMethod("inspect").invoke(op);
                                return Map.of("ok", true);
                            case "trigger":
                                {
                                    // Feed one input (idle, put_away, draw, ...) to the held gun's
                                    // animation state machine.
                                    Object display =
                                            ((java.util.Optional<?>)
                                                            Class.forName(
                                                                            "com.tacz.guns.api.TimelessAPI")
                                                                    .getMethod(
                                                                            "getGunDisplay",
                                                                            net.minecraft.world.item
                                                                                    .ItemStack
                                                                                    .class)
                                                                    .invoke(
                                                                            null,
                                                                            p.getMainHandItem()))
                                                    .orElseThrow(
                                                            () ->
                                                                    new IllegalStateException(
                                                                            "no gun display"));
                                    Object machine =
                                            display.getClass()
                                                    .getMethod("getAnimationStateMachine")
                                                    .invoke(display);
                                    if (machine == null)
                                        throw new IllegalStateException(
                                                "no animation state machine");
                                    machine.getClass()
                                            .getMethod("trigger", String.class)
                                            .invoke(machine, a[2]);
                                    return Map.of("triggered", a[2]);
                                }
                            case "stopinspect":
                                {
                                    // Remove only tracks whose running animation is an inspect clip
                                    // (inspect, inspect_empty).
                                    Object display =
                                            ((java.util.Optional<?>)
                                                            Class.forName(
                                                                            "com.tacz.guns.api.TimelessAPI")
                                                                    .getMethod(
                                                                            "getGunDisplay",
                                                                            net.minecraft.world.item
                                                                                    .ItemStack
                                                                                    .class)
                                                                    .invoke(
                                                                            null,
                                                                            p.getMainHandItem()))
                                                    .orElseThrow(
                                                            () ->
                                                                    new IllegalStateException(
                                                                            "no gun display"));
                                    Object machine =
                                            display.getClass()
                                                    .getMethod("getAnimationStateMachine")
                                                    .invoke(display);
                                    Object controller =
                                            machine.getClass()
                                                    .getMethod("getAnimationController")
                                                    .invoke(machine);
                                    List<Integer> removed = new ArrayList<>();
                                    for (Object track :
                                            (Iterable<?>)
                                                    controller
                                                            .getClass()
                                                            .getMethod("getUpdatingTrackArray")
                                                            .invoke(controller)) {
                                        int t = (Integer) track;
                                        Object runner =
                                                controller
                                                        .getClass()
                                                        .getMethod("getAnimation", int.class)
                                                        .invoke(controller, t);
                                        if (runner == null) continue;
                                        Object animation =
                                                runner.getClass()
                                                        .getMethod("getAnimation")
                                                        .invoke(runner);
                                        String name =
                                                (String)
                                                        animation
                                                                .getClass()
                                                                .getField("name")
                                                                .get(animation);
                                        if (name != null && name.startsWith("inspect")) {
                                            controller
                                                    .getClass()
                                                    .getMethod("removeAnimation", int.class)
                                                    .invoke(controller, t);
                                            removed.add(t);
                                        }
                                    }
                                    return Map.of("removedTracks", removed);
                                }
                            default:
                                throw new IllegalArgumentException(
                                        "tacz shoot|reload|aim <bool>|melee|inspect");
                        }
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("tacz не загружен: " + e);
                    }
                }
            case "sbw":
                {
                    // SuperbWarfare close strike through its own client path (sends
                    // MeleeAttackMessage): sbw melee
                    requirePlayer(p);
                    if (a.length != 2 || !a[1].equals("melee"))
                        throw new IllegalArgumentException("sbw melee");
                    try {
                        Class<?> handler =
                                Class.forName(
                                        "com.atsuishio.superbwarfare.event.ClientEventHandler");
                        Object instance = handler.getField("INSTANCE").get(null);
                        handler.getMethod(
                                        "doGunMeleeAttack",
                                        net.minecraft.world.entity.player.Player.class,
                                        double.class,
                                        double.class)
                                .invoke(instance, p, 90.0, 1.0);
                        return Map.of("ok", true);
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("superbwarfare не загружен: " + e);
                    }
                }
            case "lr":
                {
                    // LR Tactical starts melee swings from raw GLFW mouse events, not KeyMapping
                    // clicks:
                    // lr mouse <button 0|1> <action 1 press|0 release> calls its own
                    // AttackKeys.onMousePre.
                    requirePlayer(p);
                    if (a.length != 4 || !a[1].equals("mouse"))
                        throw new IllegalArgumentException("lr mouse <button> <action>");
                    try {
                        Object consumed =
                                Class.forName("me.xjqsh.lrtactical.client.input.AttackKeys")
                                        .getMethod("onMousePre", int.class, int.class)
                                        .invoke(
                                                null,
                                                Integer.parseInt(a[2]),
                                                Integer.parseInt(a[3]));
                        return Map.of("consumed", String.valueOf(consumed));
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("lrtactical не загружен: " + e);
                    }
                }
            case "forge":
                {
                    // Test-only: forge a client->server packet for a mod, straight through Fabric's
                    // C2S send,
                    // to prove a server-side network fix ignores client-supplied identity.
                    // Rig-only, never shipped.
                    requirePlayer(p);
                    if (a.length < 2) throw new IllegalArgumentException("forge <kind> <args...>");
                    String kind = a[1];
                    if (kind.equals("use_block")) {
                        // forge use_block <x> <y> <z> [face=up]: vanilla ServerboundUseItemOnPacket
                        // straight into the
                        // connection, past the client's UseBlockCallback -- what a modified client
                        // sends.
                        BlockPos pos =
                                new BlockPos(
                                        Integer.parseInt(a[2]),
                                        Integer.parseInt(a[3]),
                                        Integer.parseInt(a[4]));
                        net.minecraft.core.Direction face =
                                a.length > 5
                                        ? net.minecraft.core.Direction.byName(a[5])
                                        : net.minecraft.core.Direction.UP;
                        var hit = new BlockHitResult(Vec3.atCenterOf(pos), face, pos, false);
                        mc.getConnection()
                                .send(
                                        new net.minecraft.network.protocol.game
                                                .ServerboundUseItemOnPacket(
                                                net.minecraft.world.InteractionHand.MAIN_HAND,
                                                hit,
                                                0));
                        return Map.of(
                                "sent", kind, "pos", pos.toShortString(), "face", face.getName());
                    }
                    Object payload;
                    try {
                        switch (kind) {
                            case "parcool_stamina":
                                {
                                    // forge parcool_stamina <uuid> <value> [max=2000]
                                    // [exhausted=false]
                                    java.util.UUID id = java.util.UUID.fromString(a[2]);
                                    int value = Integer.parseInt(a[3]);
                                    int max = a.length > 4 ? Integer.parseInt(a[4]) : 2000;
                                    boolean exhausted = a.length > 5 && Boolean.parseBoolean(a[5]);
                                    Object stamina =
                                            construct(
                                                    "com.alrex.parcool.common.attachment.common.ReadonlyStamina",
                                                    exhausted,
                                                    value,
                                                    max);
                                    payload =
                                            construct(
                                                    "com.alrex.parcool.common.network.payload.StaminaPayload",
                                                    id,
                                                    stamina);
                                    break;
                                }
                            case "parcool_action":
                                {
                                    // forge parcool_action <uuid> -- empty action list is the
                                    // minimal valid body
                                    java.util.UUID id = java.util.UUID.fromString(a[2]);
                                    payload =
                                            construct(
                                                    "com.alrex.parcool.common.network.payload.ActionStatePayload",
                                                    id,
                                                    new ArrayList<>());
                                    break;
                                }
                            case "sbw_melee":
                                {
                                    // forge sbw_melee <uuid>[,<uuid>...]
                                    List<java.util.UUID> ids = new ArrayList<>();
                                    for (String s : a[2].split(","))
                                        ids.add(java.util.UUID.fromString(s));
                                    payload =
                                            construct(
                                                    "com.atsuishio.superbwarfare.network.message.send.MeleeAttackMessage",
                                                    ids);
                                    break;
                                }
                            case "sbw_vehicle_fire":
                                {
                                    // forge sbw_vehicle_fire <shooterVehicleId|-> <weaponName|-> [x
                                    // y z] -- uuid null; targetPos
                                    // only with x y z (the server uses weaponName only for a map
                                    // strike, i.e. with targetPos)
                                    Integer vid = a[2].equals("-") ? null : Integer.valueOf(a[2]);
                                    String weapon = a[3].equals("-") ? null : a[3];
                                    org.joml.Vector3f target =
                                            a.length > 6
                                                    ? new org.joml.Vector3f(
                                                            Float.parseFloat(a[4]),
                                                            Float.parseFloat(a[5]),
                                                            Float.parseFloat(a[6]))
                                                    : null;
                                    payload =
                                            construct(
                                                    "com.atsuishio.superbwarfare.network.message.send.VehicleFireMessage",
                                                    null,
                                                    target,
                                                    weapon,
                                                    vid);
                                    break;
                                }
                            case "sbw_lunge":
                                {
                                    // forge sbw_lunge 0 <uuid>  |  forge sbw_lunge 1 <x> <y> <z>
                                    int type = Integer.parseInt(a[2]);
                                    java.util.UUID id =
                                            type == 0
                                                    ? java.util.UUID.fromString(a[3])
                                                    : new java.util.UUID(0L, 0L);
                                    Vec3 pos =
                                            type == 1
                                                    ? new Vec3(
                                                            Double.parseDouble(a[3]),
                                                            Double.parseDouble(a[4]),
                                                            Double.parseDouble(a[5]))
                                                    : Vec3.ZERO;
                                    payload =
                                            construct(
                                                    "com.atsuishio.superbwarfare.network.message.send.LungeMineAttackMessage",
                                                    type,
                                                    id,
                                                    pos);
                                    break;
                                }
                            case "sbw_skin":
                                {
                                    // forge sbw_skin <entityId> <skinId>
                                    payload =
                                            construct(
                                                    "com.atsuishio.superbwarfare.network.message.send.SetVehicleSkinMessage",
                                                    Integer.parseInt(a[2]),
                                                    a[3]);
                                    break;
                                }
                            case "sbw_laser":
                                {
                                    // forge sbw_laser <uuid> <damage> [headshot=false]
                                    java.util.UUID id = java.util.UUID.fromString(a[2]);
                                    double damage = Double.parseDouble(a[3]);
                                    boolean headshot = a.length > 4 && Boolean.parseBoolean(a[4]);
                                    try {
                                        payload =
                                                construct(
                                                        "com.atsuishio.superbwarfare.network.message.send.LaserShootMessage",
                                                        damage,
                                                        id,
                                                        headshot);
                                    } catch (ClassNotFoundException gone) {
                                        if (LASER_RAW_TYPE == null)
                                            throw new IllegalStateException(
                                                    "laser_shoot raw channel was not registered at"
                                                            + " init");
                                        var buf = io.netty.buffer.Unpooled.buffer();
                                        buf.writeDouble(damage);
                                        buf.writeLong(id.getMostSignificantBits());
                                        buf.writeLong(id.getLeastSignificantBits());
                                        buf.writeBoolean(headshot);
                                        byte[] body = new byte[buf.readableBytes()];
                                        buf.readBytes(body);
                                        payload = new RawPayload(LASER_RAW_TYPE, body);
                                    }
                                    break;
                                }
                            default:
                                throw new IllegalArgumentException(
                                        "forge"
                                            + " use_block|parcool_stamina|parcool_action|sbw_melee|sbw_vehicle_fire|sbw_lunge|sbw_skin|sbw_laser"
                                            + " ...");
                        }
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("forge " + kind + ": " + e, e);
                    }
                    var cpp =
                            (net.minecraft.network.protocol.common.custom.CustomPacketPayload)
                                    payload;
                    net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking.send(cpp);
                    return Map.of(
                            "sent",
                            kind,
                            "class",
                            payload.getClass().getName(),
                            "channel",
                            cpp.type().id().toString());
                }
            case "uuid":
                {
                    // uuid <name>: the entity's server UUID + entity id, to target another rig
                    // client or a vehicle.
                    requirePlayer(p);
                    for (net.minecraft.world.entity.Entity e : mc.level.entitiesForRendering()) {
                        boolean match =
                                e instanceof net.minecraft.world.entity.player.Player
                                                && e.getName().getString().equalsIgnoreCase(a[1])
                                        || e.hasCustomName()
                                                && e.getCustomName().getString().equals(a[1]);
                        if (match)
                            return Map.of(
                                    "name", a[1], "uuid", e.getUUID().toString(), "id", e.getId());
                    }
                    throw new IllegalArgumentException("no entity named " + a[1]);
                }
            case "tick":
                return Map.of("tick", tick);
            case "attempt":
                return a[1].equals("get")
                        ? Parkour.get()
                        : Parkour.start(a[1], Float.parseFloat(a[2]));
            case "sounds":
                return sounds(mc, a.length > 1 ? a[1] : "");
            case "soundlog":
                {
                    // Every sound the client starts (SoundEventListener): short ones that "sounds"
                    // misses between polls.
                    // A category at volume 0 skips the listener: use opt sound <category> 0.01 for
                    // silent logging.
                    if ("start".equals(a[1])) {
                        if (!soundListener) {
                            mc.getSoundManager()
                                    .addListener(
                                            (sound, events, range) -> {
                                                synchronized (SOUND_LOG) {
                                                    SOUND_LOG.add(
                                                            Map.of(
                                                                    "tick",
                                                                    tick,
                                                                    "id",
                                                                    String.valueOf(
                                                                            sound.getLocation()),
                                                                    "source",
                                                                    sound.getSource().getName(),
                                                                    "relative",
                                                                    sound.isRelative()));
                                                }
                                            });
                            soundListener = true;
                        }
                        synchronized (SOUND_LOG) {
                            SOUND_LOG.clear();
                        }
                        return Map.of("ok", true);
                    }
                    synchronized (SOUND_LOG) {
                        return new ArrayList<>(SOUND_LOG);
                    }
                }
            case "opt":
                {
                    // Runtime options the lightweight start (lowmem) pins: render N | fps N | sound
                    // <category> <0..1>
                    switch (a[1]) {
                        case "render" -> mc.options.renderDistance().set(Integer.parseInt(a[2]));
                        case "fps" -> mc.options.framerateLimit().set(Integer.parseInt(a[2]));
                        case "sound" ->
                                mc.options
                                        .getSoundSourceOptionInstance(
                                                net.minecraft.sounds.SoundSource.valueOf(
                                                        a[2].toUpperCase(java.util.Locale.ROOT)))
                                        .set(Double.parseDouble(a[3]));
                        default ->
                                throw new IllegalArgumentException(
                                        "opt render N | fps N | sound <category> <volume>");
                    }
                    mc.options.save();
                    mc.options.broadcastOptions();
                    return Map.of("ok", true);
                }
            case "reloadresources":
                mc.reloadResourcePacks();
                return Map.of("ok", true);
            case "record":
                // record start <name> <seconds> | record status
                return "start".equals(a[1])
                        ? FrameRecorder.start(
                                mc.gameDirectory.toPath().resolve("recordings"),
                                a[2],
                                Double.parseDouble(a[3]))
                        : FrameRecorder.status();
            case "frames":
                {
                    if ("start".equals(a[1])) {
                        FRAMES.clear();
                        framesOn = true;
                        return Map.of("ok", true);
                    }
                    framesOn = false;
                    return Map.of("samples", new ArrayList<>(FRAMES));
                }
            case "watch":
                return watch(a[1]); // watch start|get: per-tick sprint/FastRun/FOV samples
            case "entities":
                {
                    requirePlayer(p);
                    double radius = a.length > 1 ? Double.parseDouble(a[1]) : 32;
                    float partial = mc.getTimer().getGameTimeDeltaPartialTick(true);
                    List<Map<String, Object>> out = new ArrayList<>();
                    for (net.minecraft.world.entity.Entity e : mc.level.entitiesForRendering()) {
                        if (e == p || e.distanceToSqr(p) > radius * radius) continue;
                        net.minecraft.world.phys.Vec3 tickPos = e.position();
                        net.minecraft.world.phys.Vec3 renderPos = e.getPosition(partial);
                        Map<String, Object> m = new LinkedHashMap<>();
                        m.put("id", e.getId());
                        m.put("uuid", e.getUUID().toString());
                        m.put("type", e.getType().toShortString());
                        m.put("name", e.hasCustomName() ? e.getCustomName().getString() : null);
                        // "tickPos": this client tick's own position field (the lerp target).
                        // "renderPos":
                        // what actually gets drawn this frame, lerped `partial` of the way from the
                        // last
                        // tick's position to this one - i.e. what a player is really aiming at
                        // right now.
                        m.put("tickPos", List.of(r(tickPos.x), r(tickPos.y), r(tickPos.z)));
                        m.put("renderPos", List.of(r(renderPos.x), r(renderPos.y), r(renderPos.z)));
                        m.put("eyeY", r(renderPos.y + e.getEyeHeight()));
                        out.add(m);
                    }
                    return Map.of("partialTick", r(partial), "entities", out);
                }
            case "track":
                {
                    trackName = a[1].equals("off") ? null : a[1];
                    return Map.of("track", String.valueOf(trackName));
                }
            case "aimshoot":
                {
                    // One round trip instead of "entities" + "lookat" + "tacz shoot": aim at the
                    // named
                    // entity's *rendered* chest this exact frame, then fire immediately - no extra
                    // client/network round trip can go stale between reading the target and
                    // shooting.
                    requirePlayer(p);
                    String name = a[1];
                    net.minecraft.world.entity.Entity target = findTarget(mc, name);
                    if (target == null) return Map.of("error", "no entity named " + name);
                    net.minecraft.world.phys.Vec3 aimPoint = aimAt(mc, p, target);
                    try {
                        Object op =
                                Class.forName(
                                                "com.tacz.guns.api.client.gameplay.IClientPlayerGunOperator")
                                        .getMethod("fromLocalPlayer", LocalPlayer.class)
                                        .invoke(null, p);
                        String result = String.valueOf(op.getClass().getMethod("shoot").invoke(op));
                        return Map.of(
                                "result",
                                result,
                                "aimAt",
                                List.of(r(aimPoint.x), r(aimPoint.y), r(aimPoint.z)));
                    } catch (ReflectiveOperationException e) {
                        throw new IllegalStateException("tacz " + e);
                    }
                }
            case "workshopai":
                return workshopAi(mc, a, line);
            case "hitboxes", "nudge", "uniform", "skinparts":
                return AcceptanceProbes.command(mc, a);
            case "playergeometry":
                return RenderedPlayerGeometry.command(mc, a);
            default:
                return Map.of(
                        "error",
                        "unknown command: " + cmd,
                        "commands",
                        "state widgets bfmenu keys press <key> [ticks] use [ticks] use_on <x> <y>"
                            + " <z> <face> sneak on|off scroll <dy> [dx] attack hold <mapping>"
                            + " <ticks> look <yaw> <pitch> lookat <x> <y> <z> block <x> <y> <z>"
                            + " slots slotclick <slot> [button] [type] slot <n> chat <text>"
                            + " messages [n] clearmessages screenshot <name> closescreen respawn"
                            + " disconnect connect <host:port> bfaction <action> tacz rawinput"
                            + " key|mouse_down|mouse_up|click|move_to shoot|reload|aim <bool> tick"
                            + " entities [radius] uuid <name> forge <kind> <args...> workshopai"
                            + " [state|blueprint|status|draft|generate|apply|cancel|undo|audio]"
                            + " playergeometry select <UUID>|state|clear hitboxes on|off|state"
                            + " nudge <dx> <dy> <dz> skinparts [mask] uniform"
                            + " state|open|cosmetics|toggle|take|insert");
        }
    }

    /**
     * Holds a mapping down until {@code ticks} client ticks from now (released by the END tick
     * handler).
     */
    static void hold(KeyMapping m, int ticks) {
        m.setDown(true);
        HELD_MAPPINGS.merge(m, tick + ticks, Math::max);
    }

    static KeyMapping mappingOf(Minecraft mc, String name) {
        for (KeyMapping m : mc.options.keyMappings) if (m.getName().equals(name)) return m;
        throw new IllegalArgumentException("no key mapping " + name);
    }

    private static void requirePlayer(LocalPlayer p) {
        if (p == null) throw new IllegalStateException("not in a world");
    }

    private static final List<String> FRAMES = new ArrayList<>();
    private static boolean framesOn;

    /**
     * One line per rendered frame and SBW drone (FPV addon or SBW's own): what is drawn this frame,
     * position and attitude at the frame's partial tick; plus one "cam" line per frame with the
     * rendered view's offset from the player's aim (camera mods). `frames start|get`.
     */
    private static void sampleFrame(float partial) {
        frame++;
        if (!framesOn) return;
        Minecraft mc = Minecraft.getInstance();
        if (mc.level == null) return;
        if (mc.player != null) {
            // "<us> <tick> <partial> cam <camera pitch - player pitch> <camera yaw - player yaw>
            // <player pitch>
            //  <Camera Overhaul pitch tilt> <its yaw tilt>" -- the last two are what shots add
            // (Blockfield mod).
            var camera = mc.gameRenderer.getMainCamera();
            float[] co = cameraOverhaulTilt();
            FRAMES.add(
                    System.nanoTime() / 1000
                            + " "
                            + tick
                            + " "
                            + partial
                            + " cam "
                            + r(camera.getXRot() - mc.player.getViewXRot(partial))
                            + " "
                            + r(
                                    net.minecraft.util.Mth.wrapDegrees(
                                            camera.getYRot() - mc.player.getViewYRot(partial)))
                            + " "
                            + r(mc.player.getViewXRot(partial))
                            + " "
                            + r(co[0])
                            + " "
                            + r(co[1]));
        }
        for (net.minecraft.world.entity.Entity e : mc.level.entitiesForRendering()) {
            String id = BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString();
            if (!id.endsWith("drone")) continue;
            net.minecraft.world.phys.Vec3 at = e.getPosition(partial);
            String att = "";
            try {
                att =
                        " "
                                + e.getClass().getMethod("getYaw", float.class).invoke(e, partial)
                                + " "
                                + e.getClass()
                                        .getMethod("getBodyPitch", float.class)
                                        .invoke(e, partial)
                                + " "
                                + e.getClass().getMethod("getRoll", float.class).invoke(e, partial);
            } catch (ReflectiveOperationException ignored) {
            }
            FRAMES.add(
                    System.nanoTime() / 1000
                            + " "
                            + tick
                            + " "
                            + partial
                            + " "
                            + e.getId()
                            + " "
                            + id
                            + " "
                            + r(at.x)
                            + " "
                            + r(at.y)
                            + " "
                            + r(at.z)
                            + att);
        }
        if (FRAMES.size() >= 60000) framesOn = false;
    }

    public static void recordGunEvent(String event, net.minecraft.world.item.ItemStack stack) {
        recordGunEvent(event, stack, null);
    }

    public static void recordGunEvent(
            String event,
            net.minecraft.world.item.ItemStack stack,
            net.minecraft.world.item.ItemDisplayContext context) {
        if (!renderProbe) return;
        Minecraft mc = Minecraft.getInstance();
        RENDER_EVENTS.add(
                Map.of(
                        "event",
                        event,
                        "tick",
                        tick,
                        "frame",
                        frame,
                        "nanoTime",
                        System.nanoTime(),
                        "gun",
                        gunId(stack),
                        "held",
                        gunId(
                                mc.player == null
                                        ? net.minecraft.world.item.ItemStack.EMPTY
                                        : mc.player.getMainHandItem()),
                        "context",
                        context == null ? "" : context.name(),
                        "offhand",
                        gunId(
                                mc.player == null
                                        ? net.minecraft.world.item.ItemStack.EMPTY
                                        : mc.player.getOffhandItem()),
                        "caller",
                        context == null
                                ? List.of()
                                : StackWalker.getInstance()
                                        .walk(
                                                frames ->
                                                        frames.skip(1)
                                                                .limit(8)
                                                                .map(Object::toString)
                                                                .toList())));
        if (RENDER_EVENTS.size() >= 10000) renderProbe = false;
    }

    public static Boolean rawKeyPressed(long window, boolean mouse, int code) {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null || mc.getWindow() == null || window != mc.getWindow().getWindow())
            return null;
        return RAW_INPUT.pressed(
                window, new RawInputState.Key(mouse, code), tick, System.nanoTime());
    }

    public static boolean rawInputActive() {
        Minecraft mc = Minecraft.getInstance();
        return mc != null
                && mc.getWindow() != null
                && RAW_INPUT.active(mc.getWindow().getWindow(), System.nanoTime());
    }

    public static int[] rawCursor(long window) {
        Minecraft mc = Minecraft.getInstance();
        if (mc == null || mc.getWindow() == null || window != mc.getWindow().getWindow())
            return null;
        return RAW_INPUT.cursor(window, System.nanoTime());
    }

    private static void releaseRawInput(Minecraft mc, RawInputState.Key key) {
        long window = mc.getWindow().getWindow();
        if (key.mouse())
            ((MouseHandlerInvoker) mc.mouseHandler)
                    .bf$onPress(window, key.code(), GLFW.GLFW_RELEASE, 0);
        else mc.keyboardHandler.keyPress(window, key.code(), 0, GLFW.GLFW_RELEASE, 0);
    }

    private static void resetRawInput(Minecraft mc) {
        for (RawInputState.Key key : RAW_INPUT.reset()) releaseRawInput(mc, key);
    }

    private static Object rawInput(Minecraft mc, String[] args) {
        if (args.length < 3)
            throw new IllegalArgumentException(
                    "rawinput key|combo|mouse_down|mouse_up|click|move_to");
        long window = mc.getWindow().getWindow();
        String operation = args[1];
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("op", operation);
        switch (operation) {
            case "key" -> {
                if (args.length != 3) throw new IllegalArgumentException("rawinput key <name>");
                InputConstants.Key key = InputConstants.getKey(RawInputState.keyName(args[2]));
                if (key.getValue() < 0 || key.getValue() > 348)
                    throw new IllegalArgumentException("unknown raw key");
                RawInputState.Key input = new RawInputState.Key(false, key.getValue());
                RAW_INPUT.activate(window, System.nanoTime());
                if (RAW_INPUT.hold(input, 2, tick, System.nanoTime())) {
                    mc.keyboardHandler.keyPress(window, key.getValue(), 0, GLFW.GLFW_PRESS, 0);
                    if (key.getValue() >= 32 && key.getValue() <= 126)
                        ((KeyboardHandlerInvoker) mc.keyboardHandler)
                                .bf$charTyped(window, Character.toLowerCase(key.getValue()), 0);
                }
                result.put("key", args[2]);
            }
            case "combo" -> {
                if (args.length != 3)
                    throw new IllegalArgumentException("rawinput combo <key+key>");
                List<String> names = RawInputState.comboNames(args[2]);
                int[] codes = new int[names.size()];
                for (int i = 0; i < codes.length; i++) {
                    codes[i] = InputConstants.getKey(names.get(i)).getValue();
                    if (codes[i] < 0 || codes[i] > 348)
                        throw new IllegalArgumentException("unknown raw key");
                }
                RAW_INPUT.activate(window, System.nanoTime());
                // Earlier keys are held longer: the last one releases first, as typed by hand,
                // and Minecraft polls the held ones (F3 for F3+T) while the last key is pressed.
                for (int i = 0; i < codes.length; i++)
                    RAW_INPUT.hold(
                            new RawInputState.Key(false, codes[i]),
                            1 + codes.length - i,
                            tick,
                            System.nanoTime());
                int modifiers = 0;
                for (int code : codes) {
                    mc.keyboardHandler.keyPress(window, code, 0, GLFW.GLFW_PRESS, modifiers);
                    modifiers |= RawInputState.modifier(code);
                }
                result.put("keys", args[2]);
            }
            case "mouse_down", "mouse_up", "click" -> {
                if (args.length != 3)
                    throw new IllegalArgumentException("rawinput " + operation + " <button1..3>");
                int button = Integer.parseInt(args[2]);
                int code = RawInputState.mouseCode(button);
                RawInputState.Key input = new RawInputState.Key(true, code);
                RAW_INPUT.activate(window, System.nanoTime());
                if (operation.equals("mouse_up")) {
                    RAW_INPUT.release(input);
                    releaseRawInput(mc, input);
                } else if (RAW_INPUT.hold(
                        input,
                        operation.equals("click") ? 2 : RawInputState.HOLD_TICKS,
                        tick,
                        System.nanoTime()))
                    ((MouseHandlerInvoker) mc.mouseHandler)
                            .bf$onPress(window, code, GLFW.GLFW_PRESS, 0);
                result.put("button", button);
            }
            case "move_to" -> {
                if (args.length != 4)
                    throw new IllegalArgumentException("rawinput move_to <x> <y>");
                int x = Integer.parseInt(args[2]), y = Integer.parseInt(args[3]);
                RAW_INPUT.move(
                        x, y, mc.getWindow().getScreenWidth(), mc.getWindow().getScreenHeight());
                RAW_INPUT.activate(window, System.nanoTime());
                ((MouseHandlerInvoker) mc.mouseHandler).bf$onMove(window, x, y);
                result.put("x", x);
                result.put("y", y);
                result.put("width", mc.getWindow().getScreenWidth());
                result.put("height", mc.getWindow().getScreenHeight());
            }
            default -> throw new IllegalArgumentException("invalid rawinput operation");
        }
        result.put("ok", true);
        result.put("leaseMillis", RawInputState.LEASE_NANOS / 1_000_000);
        result.put("tick", tick);
        return result;
    }

    private static String gunId(net.minecraft.world.item.ItemStack stack) {
        if (stack.isEmpty()) return "empty";
        try {
            Class<?> api = Class.forName("com.tacz.guns.api.item.IGun");
            Object gun =
                    api.getMethod("getIGunOrNull", net.minecraft.world.item.ItemStack.class)
                            .invoke(null, stack);
            if (gun != null)
                return String.valueOf(
                        api.getMethod("getGunId", net.minecraft.world.item.ItemStack.class)
                                .invoke(gun, stack));
        } catch (ReflectiveOperationException e) {
            return e.toString();
        }
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }

    /**
     * Camera Overhaul's view tilt as the Blockfield client adds it to shots: {pitch, yaw}, zeros
     * without it.
     */
    private static float[] cameraOverhaulTilt() {
        try {
            Class<?> aim = Class.forName("com.lunares.blockfield.client.CameraOverhaulAim");
            return new float[] {
                aim.getField("pitch").getFloat(null), aim.getField("yaw").getFloat(null)
            };
        } catch (ReflectiveOperationException e) {
            return new float[2];
        }
    }

    /**
     * Sounds the client is playing now: id, position, distance to the camera, instance volume and
     * pitch.
     */
    private static List<Map<String, Object>> sounds(Minecraft mc, String filter) {
        var engine = ((bf.testbot.mixin.SoundManagerAccessor) mc.getSoundManager()).bf$engine();
        var cam = mc.gameRenderer.getMainCamera().getPosition();
        List<Map<String, Object>> out = new ArrayList<>();
        for (var sound :
                new ArrayList<>(
                        ((bf.testbot.mixin.SoundEngineAccessor) engine).bf$channels().keySet())) {
            String id = String.valueOf(sound.getLocation());
            if (!filter.isEmpty() && !id.contains(filter)) continue;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", id);
            m.put("pos", List.of(r(sound.getX()), r(sound.getY()), r(sound.getZ())));
            m.put(
                    "dist",
                    r(
                            cam.distanceTo(
                                    new net.minecraft.world.phys.Vec3(
                                            sound.getX(), sound.getY(), sound.getZ()))));
            m.put("volume", r(sound.getVolume()));
            m.put("pitch", r(sound.getPitch()));
            m.put("looping", sound.isLooping());
            out.add(m);
        }
        return out;
    }

    private static Map<String, Object> state(Minecraft mc) {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("backend", HeadlessBackend.describe());
        s.put("screenKind", HeadlessBackend.screenKind(mc.screen));
        s.put("loadingOverlay", mc.getOverlay() != null);
        s.put("tick", tick);
        s.put("screen", mc.screen == null ? null : mc.screen.getClass().getSimpleName());
        s.put("connected", mc.getConnection() != null);
        s.put("windowActive", mc.isWindowActive());
        s.put("mouseGrabbed", mc.mouseHandler.isMouseGrabbed());
        s.put("overlay", mc.getOverlay() != null);
        s.put("useBinding", mc.options.keyUse.saveString());
        s.put("useDown", mc.options.keyUse.isDown());
        s.put("useClicks", ((KeyMappingAccessor) mc.options.keyUse).bf$clickCount());
        s.put("rightClickDelay", ((MinecraftInputAccess) mc).bf$rightClickDelay());
        s.put("camera", mc.options.getCameraType().name());
        if (mc.getCameraEntity() != null) {
            var view = mc.gameRenderer.getMainCamera().getPosition();
            s.put("view", List.of(r(view.x), r(view.y), r(view.z)));
            s.put("cameraEntity", mc.getCameraEntity().getType().toShortString());
        }
        s.put("fps", mc.getFps());
        // Server-measured round trip as the tab list shows it (what TACZ lag compensation uses for
        // this player).
        if (mc.getConnection() != null && mc.player != null) {
            var info = mc.getConnection().getPlayerInfo(mc.player.getUUID());
            if (info != null) s.put("ping", info.getLatency());
        }
        s.put(
                "guiSize",
                List.of(mc.getWindow().getGuiScaledWidth(), mc.getWindow().getGuiScaledHeight()));
        s.put("server", mc.getCurrentServer() == null ? null : mc.getCurrentServer().ip);
        LocalPlayer p = mc.player;
        if (p == null) return s;
        s.put("name", p.getGameProfile().getName());
        s.put("dimension", p.level().dimension().location().toString());
        s.put("pos", List.of(r(p.getX()), r(p.getY()), r(p.getZ())));
        s.put("yaw", r(p.getYRot()));
        s.put("pitch", r(p.getXRot()));
        // Rendered view direction (crosshair) after camera mods; bullets follow yaw/pitch above.
        var camera = mc.gameRenderer.getMainCamera();
        s.put("camYaw", r(camera.getYRot()));
        s.put("camPitch", r(camera.getXRot()));
        float[] co = cameraOverhaulTilt();
        s.put("coPitch", r(co[0]));
        s.put("coYaw", r(co[1]));
        s.put("health", p.getHealth());
        s.put("maxHealth", p.getMaxHealth());
        s.put("food", p.getFoodData().getFoodLevel());
        s.put("gamemode", mc.gameMode == null ? null : mc.gameMode.getPlayerMode().getName());
        s.put("slot", p.getInventory().selected);
        s.put("held", BuiltInRegistries.ITEM.getKey(p.getMainHandItem().getItem()).toString());
        s.put("offhand", BuiltInRegistries.ITEM.getKey(p.getOffhandItem().getItem()).toString());
        s.put("sprinting", p.isSprinting());
        s.put("sneaking", p.isShiftKeyDown());
        s.put("onGround", p.onGround());
        s.put("usingItem", p.isUsingItem());
        s.put("handsBusy", p.isHandsBusy());
        s.put("destroyingBlock", mc.gameMode != null && mc.gameMode.isDestroying());
        s.put("effects", p.getActiveEffects().size());
        try {
            Object stamina =
                    Class.forName("com.alrex.parcool.api.Stamina")
                            .getMethod("get", net.minecraft.world.entity.player.Player.class)
                            .invoke(null, p);
            s.put("stamina", stamina.getClass().getMethod("getValue").invoke(stamina));
            s.put("exhausted", stamina.getClass().getMethod("isExhausted").invoke(stamina));
            Class<?> hud = Class.forName("com.lunares.blockfield.client.render.HudRenderer");
            s.put("matchMap", hud.getMethod("matchMap").invoke(null));
            s.put(
                    "visiblePlayers",
                    mc.level.players().stream()
                            .map(other -> other.getGameProfile().getName())
                            .toList());
        } catch (ReflectiveOperationException e) {
            s.put("diagnosticsError", e.toString());
        }
        HitResult hit = mc.hitResult;
        if (hit instanceof BlockHitResult b && hit.getType() == HitResult.Type.BLOCK) {
            s.put(
                    "target",
                    Map.of(
                            "type",
                            "block",
                            "pos",
                            b.getBlockPos().toShortString(),
                            "state",
                            mc.level.getBlockState(b.getBlockPos()).toString()));
        } else if (hit instanceof EntityHitResult e) {
            s.put(
                    "target",
                    Map.of(
                            "type",
                            "entity",
                            "entity",
                            e.getEntity().getType().toShortString(),
                            "name",
                            e.getEntity().getName().getString()));
        } else {
            s.put("target", null);
        }
        return s;
    }

    /** Captures only the server-approved packet accepted by the native preview decoder. */
    public static void captureWorkshopAiPreview(Object packet) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            approvedBlueprint = null;
            return;
        }
        try {
            Object owner = packet.getClass().getMethod("ownerId").invoke(packet);
            if (!(owner instanceof UUID ownerId)) {
                approvedBlueprint = null;
                return;
            }
            if (!ownerId.equals(mc.player.getUUID())) return;
            approvedBlueprint = null;
            Object id = packet.getClass().getMethod("jobId").invoke(packet);
            Object position = packet.getClass().getMethod("origin").invoke(packet);
            Object raw = packet.getClass().getMethod("plan").invoke(packet);
            if (!(id instanceof UUID jobId)
                    || jobId.equals(new UUID(0, 0))
                    || !(position instanceof BlockPos origin)
                    || Math.abs((long) origin.getX()) > 30_000_000
                    || Math.abs((long) origin.getZ()) > 30_000_000
                    || Math.abs((long) origin.getY()) > 2048
                    || !(raw instanceof String plan)
                    || plan.isBlank()
                    || plan.length() > 65536) return;
            Class<?> effects =
                    Class.forName("com.lunares.blockfield.client.workshop.WorkshopAiEffects");
            if (!Boolean.TRUE.equals(
                    effects.getMethod("hasPreview", UUID.class, UUID.class)
                            .invoke(null, ownerId, jobId))) return;
            // The blueprint packet precedes state, so its job ID is checked only at query time.
            approvedBlueprint =
                    new ApprovedBlueprint(
                            ownerId,
                            jobId,
                            List.of(origin.getX(), origin.getY(), origin.getZ()),
                            plan);
        } catch (ReflectiveOperationException | RuntimeException invalid) {
            approvedBlueprint = null;
        }
    }

    private static Object workshopAi(Minecraft mc, String[] args, String line) {
        String action = args.length < 2 ? "state" : args[1];
        String[] parts = line.split("\\s+", 3);
        String value = parts.length < 3 ? "" : parts[2];
        try {
            Class<?> client =
                    Class.forName("com.lunares.blockfield.client.workshop.WorkshopAiClient");
            Map<String, Object> result = new LinkedHashMap<>();
            switch (action) {
                case "state", "blueprint" -> {}
                case "draft" -> {
                    if (value.length() > 4096)
                        throw new IllegalArgumentException("Prompt exceeds 4096 characters");
                    if (parts.length == 3)
                        client.getMethod("draft", String.class).invoke(null, value);
                }
                case "audio" ->
                        result.put(
                                "sent",
                                client.getMethod("transcribe", byte[].class)
                                        .invoke(null, loadAndValidateWav(mc, value)));
                case "status", "generate", "apply", "cancel", "undo" -> {
                    String prompt = "";
                    if (action.equals("generate")) {
                        prompt =
                                parts.length == 3
                                        ? value
                                        : (String) client.getMethod("draft").invoke(null);
                        if (prompt.isBlank() || prompt.length() > 4096)
                            throw new IllegalArgumentException(
                                    "Generate requires 1..4096 characters");
                        client.getMethod("draft", String.class).invoke(null, prompt);
                    }
                    result.put(
                            "sent",
                            client.getMethod("request", String.class, String.class)
                                    .invoke(null, action, prompt));
                }
                default ->
                        throw new IllegalArgumentException(
                                "workshopai state|blueprint|draft [text]|status|generate"
                                        + " [prompt]|apply|cancel|undo|audio <owned-game-file>");
            }
            for (String name : List.of("available", "throttled", "uploading", "draft"))
                result.put(name, client.getMethod(name).invoke(null));
            Object state = client.getMethod("state").invoke(null);
            for (var component : state.getClass().getRecordComponents()) {
                Object field = component.getAccessor().invoke(state);
                if (field instanceof BlockPos position)
                    field = List.of(position.getX(), position.getY(), position.getZ());
                result.put(component.getName(), field);
            }
            Class<?> effects =
                    Class.forName("com.lunares.blockfield.client.workshop.WorkshopAiEffects");
            result.put(
                    "hasPreview",
                    effects.getMethod("hasPreview", UUID.class, UUID.class)
                            .invoke(null, result.get("ownerId"), result.get("jobId")));
            if (action.equals("blueprint")) {
                ApprovedBlueprint capture = approvedBlueprint;
                if (capture == null
                        || mc.player == null
                        || !capture.ownerId().equals(mc.player.getUUID())
                        || !capture.ownerId().equals(result.get("ownerId"))
                        || !capture.jobId().equals(result.get("jobId"))
                        || !Boolean.TRUE.equals(result.get("hasPreview"))) {
                    return Map.of(
                            "error", "Approved workshop AI blueprint is unavailable",
                            "errorCode", "blueprint_missing");
                }
                return Map.of(
                        "ownerId", capture.ownerId().toString(),
                        "jobId", capture.jobId().toString(),
                        "origin", capture.origin(),
                        "plan", capture.plan());
            }
            return result;
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("Workshop AI contract changed or mod is absent", e);
        }
    }

    private static byte[] loadAndValidateWav(Minecraft mc, String value) {
        byte[] data;
        try {
            Path game = mc.gameDirectory.toPath().toRealPath();
            Path path = game.resolve(value).toRealPath();
            if (value.isBlank() || !path.startsWith(game) || !Files.isRegularFile(path))
                throw new IllegalArgumentException(
                        "Audio fixture must be inside this game's directory");
            try (var stream = Files.newInputStream(path, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
                data = stream.readNBytes(960045);
            }
        } catch (java.io.IOException e) {
            throw new IllegalStateException("Cannot read owned audio fixture", e);
        }
        if (data.length < 46 || data.length > 960044 || (data.length - 44) % 2 != 0)
            throw new IllegalArgumentException("Expected <=30 seconds of PCM16 mono 16kHz WAV");
        ByteBuffer buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
        if (buf.getInt(0) != 0x46464952
                || buf.getInt(4) != data.length - 8
                || buf.getInt(8) != 0x45564157
                || buf.getInt(12) != 0x20746d66
                || buf.getInt(16) != 16
                || buf.getShort(20) != 1
                || buf.getShort(22) != 1
                || buf.getInt(24) != 16000
                || buf.getInt(28) != 32000
                || buf.getShort(32) != 2
                || buf.getShort(34) != 16
                || buf.getInt(36) != 0x61746164
                || buf.getInt(40) != data.length - 44)
            throw new IllegalArgumentException("Invalid PCM16 mono 16kHz WAV header");
        return data;
    }

    private static double r(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
