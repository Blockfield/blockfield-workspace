package bf.testbot;

import com.mojang.blaze3d.vertex.PoseStack;
import com.mojang.blaze3d.vertex.VertexConsumer;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.Camera;
import net.minecraft.client.Minecraft;
import net.minecraft.client.model.EntityModel;
import net.minecraft.client.model.HumanoidModel;
import net.minecraft.client.model.PlayerModel;
import net.minecraft.client.model.geom.ModelPart;
import net.minecraft.client.player.AbstractClientPlayer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.phys.Vec3;

import org.joml.Matrix4f;
import org.joml.Vector3f;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/** Observes six native skin parts plus attributed layer boxes; no server hitbox reconstruction. */
public final class RenderedPlayerGeometry {
    /** Receipt schema with per-box ids, layer sources and the unattributed list. */
    public static final String SCHEMA_V2 = "blockfield.playergeometry.v2";

    private static final long SELECTION_TTL = TimeUnit.SECONDS.toNanos(30);
    private static final long RECEIPT_TTL = TimeUnit.SECONDS.toNanos(2);
    private static final int MAX_VERTICES = 4096;
    private static final List<String> PARTS =
            List.of("head", "torso", "left_arm", "right_arm", "left_leg", "right_leg");
    private static final String BASE = "base";

    /**
     * §3.6 exclusions by layer class simple name. Cape, held items, elytra, stuck arrows/stingers,
     * worn heads, parrots, deadmau5 ears and the SBW parachute (packed renderer and open canopy
     * share the name match; packed-curios attribution is a phase-2 follow-up) are skipped, never
     * unattributed. Anything else is attributed to a bone or listed unattributed.
     */
    private static final Set<String> EXCLUDED_LAYERS =
            Set.of(
                    "CapeLayer",
                    "ItemInHandLayer",
                    "PlayerItemInHandLayer",
                    "ElytraLayer",
                    "ArrowLayer",
                    "BeeStingerLayer",
                    "StuckInBodyLayer",
                    "CustomHeadLayer",
                    "ParrotOnShoulderLayer",
                    "Deadmau5EarsLayer",
                    // Intermediary names: the testbot runs unmapped, so getSimpleName() of a
                    // vanilla layer is class_NNN (keep in sync with compare_body_pose.py).
                    "class_972",
                    "class_979",
                    "class_973",
                    "class_4506",
                    "class_976",
                    "class_983",
                    "class_978",
                    "class_989",
                    "class_5697",
                    "class_4507");

    private static UUID selected;
    private static Object selectedWorld;
    private static long selectionExpires;
    private static long capturedAt;
    private static long frameSequence;
    private static Map<String, Object> receipt;
    private static Frame frame;
    private static String unavailable = "not_selected";
    private static Field childrenField;
    private static boolean childrenFieldResolved;
    // U17 F3+B debug boxes drawn by the Blockfield client renderer for this target. Read
    // reflectively so the testbot keeps no compile dependency on the mod; absent without it.
    private static Method debugSnapshotMethod;
    private static boolean debugSnapshotResolved;

    private RenderedPlayerGeometry() {}

    /** Which bone a captured ModelPart belongs to and under which receipt id/source. */
    private record Owner(String part, String id, String source) {}

    private static final class BoxRun {
        final Owner owner;
        final List<List<Double>> vertices = new ArrayList<>();

        BoxRun(Owner owner) {
            this.owner = owner;
        }
    }

    private static final class Frame {
        final AbstractClientPlayer target;
        final Matrix4f inverseEntry;
        final Vec3 camera;
        final List<Double> position;
        final float partial;
        final long tick;
        final String skin;
        final boolean sodiumFallback;
        final Map<String, Object> pose;
        final Map<String, Object> metadata;
        final long captureStartNs;
        final IdentityHashMap<ModelPart, Owner> owners = new IdentityHashMap<>();
        final Set<Object> knownModels = Collections.newSetFromMap(new IdentityHashMap<>());
        final Map<String, List<BoxRun>> runs = new LinkedHashMap<>();
        final List<BoxRun> unattributedRuns = new ArrayList<>();
        final List<String> skippedLayers = new ArrayList<>();
        final Set<String> skippedSet = new HashSet<>();
        final Map<String, Integer> layerModels = new LinkedHashMap<>();
        final Map<String, Integer> counts = new HashMap<>();
        EntityModel<?> tracked;
        String layerSource;
        int layerDepth;
        boolean layerSkipped;
        boolean baseActive;
        boolean baseComplete;
        String failure;

        Frame(
                AbstractClientPlayer target,
                Matrix4f inverseEntry,
                Vec3 camera,
                Vec3 position,
                float partial) {
            this.target = target;
            captureStartNs = System.nanoTime();
            Minecraft mc = Minecraft.getInstance();
            Map<String, Object> observations = new LinkedHashMap<>();
            observations.put("schema", SCHEMA_V2);
            observations.put("sampleUnixMs", System.currentTimeMillis());
            observations.put("sampleMonoNs", captureStartNs);
            observations.put("dimension", target.level().dimension().location().toString());
            observations.put(
                    "observerUuid", mc.player == null ? null : mc.player.getUUID().toString());
            observations.put("observerTick", mc.player == null ? 0 : mc.player.tickCount);
            observations.put("targetTickCount", target.tickCount);
            observations.put("feet", point(target.getX(), target.getY(), target.getZ()));
            observations.put("previousFeet", point(target.xo, target.yo, target.zo));
            observations.put("cameraPosition", point(camera.x, camera.y, camera.z));
            observations.put("observedSwim", target.getSwimAmount(partial));
            observations.put("observedAttack", target.getAttackAnim(partial));
            observations.put("observedWalkPosition", target.walkAnimation.position(partial));
            observations.put("observedWalkSpeed", target.walkAnimation.speed(partial));
            metadata = Collections.unmodifiableMap(observations);
            this.inverseEntry = inverseEntry;
            this.camera = camera;
            this.position = point(position.x, position.y, position.z);
            this.partial = partial;
            tick = target.level().getGameTime();
            skin = target.getSkin().model().id();
            sodiumFallback = FabricLoader.getInstance().isModLoaded("sodium");
            pose =
                    Map.of(
                            "bodyYaw", target.yBodyRot,
                            "previousBodyYaw", target.yBodyRotO,
                            "headYaw", target.yHeadRot,
                            "previousHeadYaw", target.yHeadRotO,
                            "pitch", target.getXRot(),
                            "previousPitch", target.xRotO,
                            "pose", target.getPose().name(),
                            "mainArm", target.getMainArm().name());
        }
    }

    static Object command(Minecraft mc, String[] args) {
        if (args.length == 2 && args[1].equals("clear")) {
            clear();
            return Map.of("cleared", true);
        }
        expire(mc.level);
        if (args.length == 3 && args[1].equals("select")) {
            clear();
            UUID id = UUID.fromString(args[2]);
            if (!id.toString().equals(args[2]))
                throw new IllegalArgumentException("expected canonical target UUID");
            if (mc.level == null || !(mc.level.getPlayerByUUID(id) instanceof AbstractClientPlayer))
                return missing("target_not_loaded");
            selected = id;
            selectedWorld = mc.level;
            selectionExpires = System.nanoTime() + SELECTION_TTL;
            unavailable = "awaiting_render";
            return Map.of("targetUuid", id.toString(), "selectionSeconds", 30);
        }
        if (args.length == 2 && args[1].equals("state")) {
            if (receipt != null) return receipt;
            return missing(unavailable);
        }
        throw new IllegalArgumentException("playergeometry select <UUID>|state|clear");
    }

    private static Map<String, Object> missing(String reason) {
        return Map.of(
                "error", "Native player geometry unavailable",
                "errorCode", "geometry_missing",
                "reason", reason);
    }

    public static void clear() {
        selected = null;
        selectedWorld = null;
        receipt = null;
        frame = null;
        unavailable = "not_selected";
    }

    static void expire(Object world) {
        if (selected != null && (world != selectedWorld || System.nanoTime() >= selectionExpires))
            clear();
        if (receipt != null && System.nanoTime() - capturedAt > RECEIPT_TTL) {
            receipt = null;
            unavailable = "stale_render";
        }
    }

    public static void begin(
            Entity entity,
            double x,
            double y,
            double z,
            float partial,
            PoseStack poses,
            Camera camera) {
        if (selected == null || !selected.equals(entity.getUUID())) return;
        expire(entity.level());
        if (selected == null) return;
        receipt = null;
        frame = null;
        unavailable = "incomplete_render";
        if (!(entity instanceof AbstractClientPlayer target)
                || camera == null
                || !camera.isInitialized()
                || !Float.isFinite(partial)
                || partial < 0
                || partial > 1
                || !finite(target.yBodyRot, target.yBodyRotO, target.yHeadRot)
                || !finite(target.yHeadRotO, target.getXRot(), target.xRotO)) return;
        Vec3 cameraPosition = camera.getPosition();
        Vec3 position = cameraPosition.add(x, y, z);
        // Dispatcher calls from inventory previews use a different origin than the world camera.
        if (!finite(position.x, position.y, position.z)
                || position.distanceToSqr(target.getPosition(partial)) > 4) return;
        Matrix4f entry = new Matrix4f(poses.last().pose());
        float determinant = entry.determinant();
        if (!entry.isFinite() || !Float.isFinite(determinant) || Math.abs(determinant) < 1e-8)
            return;
        frame = new Frame(target, entry.invert(), cameraPosition, position, partial);
    }

    public static boolean beginModel(EntityModel<?> model) {
        Frame current = frame;
        if (current == null) return false;
        if (!(model instanceof PlayerModel<?> player)
                || current.baseActive
                || current.baseComplete) {
            current.failure = "unsupported_base_pass";
            return false;
        }
        current.tracked = player;
        current.knownModels.add(player);
        registerTree(current.owners, player.head, "head", "head", BASE);
        registerTree(current.owners, player.body, "torso", "torso", BASE);
        registerTree(current.owners, player.leftArm, "left_arm", "left_arm", BASE);
        registerTree(current.owners, player.rightArm, "right_arm", "right_arm", BASE);
        registerTree(current.owners, player.leftLeg, "left_leg", "left_leg", BASE);
        registerTree(current.owners, player.rightLeg, "right_leg", "right_leg", BASE);
        registerTree(current.owners, player.hat, "head", "hat", BASE);
        registerTree(current.owners, player.jacket, "torso", "jacket", BASE);
        registerTree(current.owners, player.leftSleeve, "left_arm", "left_sleeve", BASE);
        registerTree(current.owners, player.rightSleeve, "right_arm", "right_sleeve", BASE);
        registerTree(current.owners, player.leftPants, "left_leg", "left_pants", BASE);
        registerTree(current.owners, player.rightPants, "right_leg", "right_pants", BASE);
        for (String part : PARTS) current.runs.put(part, new ArrayList<>());
        current.baseActive = true;
        return true;
    }

    public static void endModel(boolean success) {
        if (frame == null) return;
        frame.baseActive = false;
        frame.baseComplete = success;
        if (!success) frame.failure = "incomplete_base_pass";
    }

    /**
     * Opens a layer scope around one RenderLayer.render inside LivingEntityRenderer.render. Returns
     * false when no capture runs, in which case the caller must skip endLayer.
     */
    public static boolean beginLayer(Entity entity, Object layer) {
        Frame current = frame;
        if (current == null || current.target != entity || current.failure != null) return false;
        String source = layerSource(layer);
        if (current.layerDepth == 0) {
            current.layerSource = source;
            current.layerSkipped = excludedLayer(layer.getClass().getSimpleName());
            if (current.layerSkipped && current.skippedSet.add(layer.getClass().getSimpleName()))
                current.skippedLayers.add(layer.getClass().getSimpleName());
        } else if (!source.equals(current.layerSource)) {
            current.failure = "nested_layer";
            return false;
        }
        current.layerDepth++;
        return true;
    }

    public static void endLayer(Entity entity, Object layer) {
        Frame current = frame;
        if (current == null || current.target != entity || current.layerDepth == 0) return;
        if (--current.layerDepth == 0) {
            current.layerSource = null;
            current.layerSkipped = false;
        }
    }

    /** Registers a layer model's bones when it inherits the tracked model's pose. */
    public static void copyLayerModel(Object from, HumanoidModel<?> to) {
        Frame current = frame;
        if (current == null
                || current.failure != null
                || current.layerSource == null
                || current.layerSkipped
                || to == null
                || !current.knownModels.contains(from)) return;
        registerHumanoid(current.owners, to, current.layerSource);
        current.knownModels.add(to);
        current.layerModels.merge(current.layerSource, 1, Integer::sum);
    }

    static String layerSource(Object layer) {
        return "layer:" + layer.getClass().getSimpleName();
    }

    static boolean excludedLayer(String simpleName) {
        return EXCLUDED_LAYERS.contains(simpleName)
                || simpleName.contains("Parachute")
                || simpleName.contains("Canopy");
    }

    static void registerHumanoid(
            Map<ModelPart, Owner> owners, HumanoidModel<?> model, String source) {
        registerTree(owners, model.head, "head", "head", source);
        registerTree(owners, model.hat, "head", "head", source);
        registerTree(owners, model.body, "torso", "torso", source);
        registerTree(owners, model.leftArm, "left_arm", "left_arm", source);
        registerTree(owners, model.rightArm, "right_arm", "right_arm", source);
        registerTree(owners, model.leftLeg, "left_leg", "left_leg", source);
        registerTree(owners, model.rightLeg, "right_leg", "right_leg", source);
    }

    static void registerTree(
            Map<ModelPart, Owner> owners, ModelPart root, String part, String id, String source) {
        if (root == null) return;
        owners.putIfAbsent(root, new Owner(part, id, source));
        for (Map.Entry<String, ModelPart> child : childrenOf(root).entrySet()) {
            if (child.getValue() == null) continue;
            registerTree(owners, child.getValue(), part, id + "/" + child.getKey(), source);
        }
    }

    static Map<String, ModelPart> childrenOf(ModelPart part) {
        try {
            if (!childrenFieldResolved) {
                childrenFieldResolved = true;
                childrenField = findChildrenField();
            }
            if (childrenField != null && childrenField.get(part) instanceof Map<?, ?> raw) {
                Map<String, ModelPart> result = new LinkedHashMap<>();
                for (Map.Entry<?, ?> entry : raw.entrySet())
                    if (entry.getKey() instanceof String name
                            && entry.getValue() instanceof ModelPart child) result.put(name, child);
                return result;
            }
        } catch (ReflectiveOperationException | RuntimeException expected) {
            // Vanilla renames keep the six bones; descendants stay unattributed and fail the gate.
        }
        return Map.of();
    }

    /**
     * The dev-named {@code children} field first; at runtime the intermediary name differs and
     * reflection strings are not remapped, so fall back to the single Map-typed field (uniform
     * cubes live in nested child parts, and without them every uniform box lands in {@code
     * unattributed}).
     */
    static Field findChildrenField() {
        try {
            Field named = ModelPart.class.getDeclaredField("children");
            named.setAccessible(true);
            return named;
        } catch (ReflectiveOperationException expected) {
            // Intermediary runtime names below.
        }
        for (Field field : ModelPart.class.getDeclaredFields()) {
            if (Map.class.isAssignableFrom(field.getType())) {
                try {
                    field.setAccessible(true);
                    return field;
                } catch (RuntimeException skipped) {
                    // Try the next Map-typed field.
                }
            }
        }
        return null;
    }

    public static VertexConsumer observe(ModelPart part, VertexConsumer original) {
        Frame current = frame;
        if (current == null || current.failure != null || current.layerSkipped) return original;
        Owner owner = current.owners.get(part);
        VertexConsumer root =
                original instanceof ObservedConsumer observed ? observed.root : original;
        BoxRun run;
        String budget;
        // Base bones count only inside the base pass: a re-render later in the frame would
        // append duplicate boxes to the same parts.
        if (owner != null
                && (owner.source.equals(BASE)
                        ? current.baseActive
                        : owner.source.equals(current.layerSource))) {
            run = new BoxRun(owner);
            current.runs.get(owner.part).add(run);
            budget = owner.part;
        } else if (owner == null && current.layerSource != null) {
            run = new BoxRun(new Owner(null, null, current.layerSource));
            current.unattributedRuns.add(run);
            budget = "unattributed";
        } else {
            // Base-pass ornaments (cloak, ear) and foreign passes are not body geometry.
            return original;
        }
        return new ObservedConsumer(root, current, run, budget);
    }

    private record ObservedConsumer(VertexConsumer root, Frame captured, BoxRun run, String budget)
            implements VertexConsumer {
        // Sodium's unknown-consumer fallback is native geometry, with test-only warning/overhead.
        @Override
        public VertexConsumer addVertex(float x, float y, float z) {
            if (captured == frame && captured.failure == null) {
                int total = captured.counts.getOrDefault(budget, 0);
                if (total >= MAX_VERTICES) captured.failure = "vertex_limit";
                else {
                    List<Double> world =
                            worldPoint(captured.inverseEntry, captured.camera, x, y, z);
                    if (!finite(world.get(0), world.get(1), world.get(2))
                            || distanceSquared(world, captured.position) > 64 * 64)
                        captured.failure = "invalid_vertex";
                    else {
                        run.vertices.add(world);
                        captured.counts.put(budget, total + 1);
                    }
                }
            }
            root.addVertex(x, y, z);
            return this;
        }

        @Override
        public VertexConsumer setColor(int r, int g, int b, int a) {
            root.setColor(r, g, b, a);
            return this;
        }

        @Override
        public VertexConsumer setUv(float u, float v) {
            root.setUv(u, v);
            return this;
        }

        @Override
        public VertexConsumer setUv1(int u, int v) {
            root.setUv1(u, v);
            return this;
        }

        @Override
        public VertexConsumer setUv2(int u, int v) {
            root.setUv2(u, v);
            return this;
        }

        @Override
        public VertexConsumer setNormal(float x, float y, float z) {
            root.setNormal(x, y, z);
            return this;
        }
    }

    public static void end(Entity entity) {
        Frame current = frame;
        if (current == null || current.target != entity) return;
        frame = null;
        if (!current.baseComplete || current.failure != null) {
            unavailable = current.failure == null ? "incomplete_render" : current.failure;
            return;
        }
        Map<String, Object> parts = new LinkedHashMap<>();
        for (String part : PARTS) {
            List<List<Double>> base = new ArrayList<>();
            List<Map<String, Object>> boxes = new ArrayList<>();
            for (BoxRun run : current.runs.get(part)) {
                if (run.owner.source.equals(BASE)) base.addAll(run.vertices);
                boxes.addAll(boxEntries(run));
            }
            if (base.isEmpty() || base.size() % 4 != 0) {
                unavailable = "incomplete_parts";
                return;
            }
            Map<String, Object> described = new LinkedHashMap<>(describe(base));
            described.put("boxes", Collections.unmodifiableList(boxes));
            parts.put(part, Collections.unmodifiableMap(described));
        }
        List<Map<String, Object>> unattributed = new ArrayList<>();
        for (BoxRun run : current.unattributedRuns) {
            if (run.vertices.isEmpty()) continue;
            Map<String, Object> entry = new LinkedHashMap<>(describe(run.vertices));
            entry.put("source", run.owner.source);
            unattributed.add(Collections.unmodifiableMap(entry));
        }
        Map<String, Object> geometry = new LinkedHashMap<>();
        geometry.put("targetUuid", current.target.getUUID().toString());
        geometry.put("tick", current.tick);
        geometry.put("partialTick", current.partial);
        geometry.put("renderPosition", current.position);
        geometry.put("skinModel", current.skin);
        geometry.put("capturePath", "native_emitted_quads");
        geometry.put("sodiumFallback", current.sodiumFallback);
        geometry.put("nativePose", current.pose);
        geometry.put("parts", Collections.unmodifiableMap(parts));
        geometry.put("unattributed", Collections.unmodifiableList(unattributed));
        geometry.put(
                "skippedLayers", Collections.unmodifiableList(List.copyOf(current.skippedLayers)));
        geometry.put("layerModels", new LinkedHashMap<>(current.layerModels));
        Map<String, Object> debugBoxes = debugSnapshot(entity.getId());
        if (debugBoxes != null) geometry.put("debugBoxes", debugBoxesForReceipt(debugBoxes));
        receipt = completeReceipt(current.metadata, geometry, current.captureStartNs);
        capturedAt = System.nanoTime();
    }

    /**
     * Boxes the F3+B overlay drew for this entity: {@code tick} (client draw tick), {@code
     * serverTick} (the committed server frame the boxes were drawn from), {@code shapeKey} and
     * {@code boxes} (part name to boxes to 24 world doubles). §9.1(10) pairs them with the server
     * dump by {@code serverTick}. Null when the overlay drew nothing (channel absent, unsubscribed,
     * or no server frame).
     */
    static Map<String, Object> debugSnapshot(int entityId) {
        try {
            if (!debugSnapshotResolved) {
                debugSnapshotResolved = true;
                try {
                    debugSnapshotMethod =
                            Class.forName(
                                            "com.lunares.blockfield.client.render.BodyPartHitboxRenderer")
                                    .getMethod("debugSnapshot", int.class);
                } catch (ReflectiveOperationException expected) {
                    debugSnapshotMethod = null;
                }
            }
            if (debugSnapshotMethod == null) return null;
            Object snapshot = debugSnapshotMethod.invoke(null, entityId);
            if (!(snapshot instanceof Map)) return null;
            @SuppressWarnings("unchecked")
            Map<String, Object> typed = (Map<String, Object>) snapshot;
            return debugBoxesForReceipt(new LinkedHashMap<>(typed));
        } catch (ReflectiveOperationException | RuntimeException expected) {
            return null;
        }
    }

    /**
     * Receipt copy of the drawn debug boxes. The copy keeps the renderer's {@code serverTick} so
     * the comparator can pair by the committed server frame instead of the client draw tick.
     */
    static Map<String, Object> debugBoxesForReceipt(Map<String, Object> snapshot) {
        return Collections.unmodifiableMap(new LinkedHashMap<>(snapshot));
    }

    /** One receipt entry per closed cube; open runs stay a single quads entry. */
    static List<Map<String, Object>> boxEntries(BoxRun run) {
        List<Map<String, Object>> entries = new ArrayList<>();
        List<List<Double>> vertices = run.vertices;
        if (!vertices.isEmpty() && vertices.size() % 24 == 0) {
            List<Map<String, Object>> split = new ArrayList<>();
            boolean closed = true;
            for (int start = 0; start < vertices.size(); start += 24) {
                List<List<Double>> cube = boxCorners(vertices.subList(start, start + 24));
                if (cube == null) {
                    closed = false;
                    break;
                }
                split.add(boxEntry(run.owner, vertices.subList(start, start + 24), cube));
            }
            if (closed && !split.isEmpty()) return split;
        }
        if (vertices.isEmpty()) return entries;
        Map<String, Object> single = new LinkedHashMap<>(describe(vertices));
        single.put("id", run.owner.id);
        single.put("source", run.owner.source);
        entries.add(Collections.unmodifiableMap(single));
        return entries;
    }

    private static Map<String, Object> boxEntry(
            Owner owner, List<List<Double>> vertices, List<List<Double>> corners) {
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("id", owner.id);
        entry.put("source", owner.source);
        entry.put("shape", "box");
        entry.put("vertices", List.copyOf(vertices));
        entry.put("corners", List.copyOf(corners));
        return Collections.unmodifiableMap(entry);
    }

    private static Map<String, Object> completeReceipt(
            Map<String, Object> metadata, Map<String, Object> geometry, long startNs) {
        long endNs = System.nanoTime();
        Map<String, Object> completed = new LinkedHashMap<>(metadata);
        completed.putAll(geometry);
        completed.put("frameSequence", ++frameSequence);
        completed.put("captureEndMonoNs", endNs);
        completed.put("captureDurationNs", endNs - startNs);
        return Collections.unmodifiableMap(completed);
    }

    private static Map<String, Object> describe(List<List<Double>> points) {
        List<List<Double>> vertices = List.copyOf(points);
        List<List<Double>> corners = boxCorners(vertices);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("shape", corners == null ? "quads" : "box");
        result.put("vertices", vertices);
        result.put("corners", corners);
        return Collections.unmodifiableMap(result);
    }

    private static List<List<Double>> boxCorners(List<List<Double>> points) {
        if (points.size() != 24) return null;
        List<List<Double>> unique = new ArrayList<>();
        List<Integer> counts = new ArrayList<>();
        List<List<Integer>> faces = new ArrayList<>();
        for (int i = 0; i < 24; i += 4) {
            List<Integer> face = new ArrayList<>();
            for (int j = 0; j < 4; j++) {
                List<Double> point = points.get(i + j);
                int index = -1;
                for (int k = 0; k < unique.size(); k++)
                    if (distanceSquared(point, unique.get(k)) < 1e-10) index = k;
                if (index < 0) {
                    index = unique.size();
                    unique.add(point);
                    counts.add(0);
                }
                counts.set(index, counts.get(index) + 1);
                if (face.contains(index)) return null;
                face.add(index);
            }
            double[] ab = subtract(points.get(i + 1), points.get(i));
            double[] ad = subtract(points.get(i + 3), points.get(i));
            double ab2 = dot(ab, ab);
            double ad2 = dot(ad, ad);
            if (ab2 < 1e-10 || ad2 < 1e-10 || Math.abs(dot(ab, ad)) > 1e-5 * Math.sqrt(ab2 * ad2))
                return null;
            for (int axis = 0; axis < 3; axis++)
                if (Math.abs(
                                points.get(i).get(axis)
                                        + ab[axis]
                                        + ad[axis]
                                        - points.get(i + 2).get(axis))
                        > 1e-5) return null;
            Collections.sort(face);
            if (faces.contains(face)) return null;
            faces.add(face);
        }
        if (unique.size() != 8 || counts.stream().anyMatch(count -> count != 3)) return null;
        List<List<Double>> neighbors = new ArrayList<>();
        for (int i = 0; i < 24; i += 4) {
            for (int j = 0; j < 4; j++) {
                if (distanceSquared(points.get(i + j), unique.get(0)) >= 1e-10) continue;
                for (int offset : new int[] {1, 3}) {
                    List<Double> neighbor = points.get(i + (j + offset) % 4);
                    if (neighbors.stream()
                            .noneMatch(other -> distanceSquared(other, neighbor) < 1e-10))
                        neighbors.add(neighbor);
                }
            }
        }
        if (neighbors.size() != 3) return null;
        double[][] edges = new double[3][];
        for (int i = 0; i < 3; i++) edges[i] = subtract(neighbors.get(i), unique.get(0));
        for (int i = 0; i < 3; i++)
            for (int j = i + 1; j < 3; j++)
                if (Math.abs(dot(edges[i], edges[j]))
                        > 1e-5 * Math.sqrt(dot(edges[i], edges[i]) * dot(edges[j], edges[j])))
                    return null;
        for (int mask = 0; mask < 8; mask++) {
            double[] corner = {unique.get(0).get(0), unique.get(0).get(1), unique.get(0).get(2)};
            for (int edge = 0; edge < 3; edge++)
                if ((mask & (1 << edge)) != 0)
                    for (int axis = 0; axis < 3; axis++) corner[axis] += edges[edge][axis];
            List<Double> expected = point(corner[0], corner[1], corner[2]);
            if (unique.stream().noneMatch(vertex -> distanceSquared(vertex, expected) < 1e-10))
                return null;
        }
        return List.copyOf(unique);
    }

    private static double[] subtract(List<Double> a, List<Double> b) {
        return new double[] {a.get(0) - b.get(0), a.get(1) - b.get(1), a.get(2) - b.get(2)};
    }

    private static double dot(double[] a, double[] b) {
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    }

    private static double distanceSquared(List<Double> a, List<Double> b) {
        double x = a.get(0) - b.get(0);
        double y = a.get(1) - b.get(1);
        double z = a.get(2) - b.get(2);
        return x * x + y * y + z * z;
    }

    private static boolean finite(double x, double y, double z) {
        return Double.isFinite(x) && Double.isFinite(y) && Double.isFinite(z);
    }

    private static List<Double> point(double x, double y, double z) {
        return List.of(x, y, z);
    }

    private static List<Double> worldPoint(
            Matrix4f inverseEntry, Vec3 camera, float x, float y, float z) {
        Vector3f relative = inverseEntry.transformPosition(new Vector3f(x, y, z));
        return point(camera.x + relative.x, camera.y + relative.y, camera.z + relative.z);
    }

    private static ModelPart plainPart(Map<String, ModelPart> children) {
        return new ModelPart(List.of(), children);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        Map<String, Object> metadata = new LinkedHashMap<>();
        metadata.put("schema", "blockfield.playergeometry.v1");
        metadata.put("sampleUnixMs", System.currentTimeMillis());
        metadata.put("sampleMonoNs", System.nanoTime());
        Map<String, Object> first =
                completeReceipt(metadata, Map.of("tick", 123L), System.nanoTime());
        long sequence = (long) first.get("frameSequence");
        clear();
        Map<String, Object> second =
                completeReceipt(metadata, Map.of("tick", 124L), System.nanoTime());
        metadata.clear();
        if (!first.get("schema").equals("blockfield.playergeometry.v1")
                || !first.get("tick").equals(123L)
                || (long) second.get("frameSequence") != sequence + 1
                || (long) first.get("captureDurationNs") < 0)
            throw new AssertionError(
                    "receipt metadata lost schema, immutable snapshot or monotonic sequence");
        try {
            first.clear();
            throw new AssertionError("mutable completed receipt metadata");
        } catch (UnsupportedOperationException expectedFailure) {
            // Completed metadata must remain a snapshot after selection renewal.
        }
        Matrix4f entry = new Matrix4f().rotateY(0.73f).rotateX(-0.32f);
        Matrix4f nativePart = new Matrix4f().translate(3, 2, -4).rotateZ(0.4f).scale(0.5f, 2, 1);
        Vector3f emitted =
                new Matrix4f(entry).mul(nativePart).transformPosition(new Vector3f(1, 2, 3));
        Vector3f expected = nativePart.transformPosition(new Vector3f(1, 2, 3));
        List<Double> actual =
                worldPoint(
                        new Matrix4f(entry).invert(),
                        new Vec3(100, 50, -70),
                        emitted.x,
                        emitted.y,
                        emitted.z);
        if (distanceSquared(actual, point(100 + expected.x, 50 + expected.y, -70 + expected.z))
                > 1e-9) throw new AssertionError("camera removal lost native root/part transforms");
        List<List<Double>> corners =
                List.of(
                        point(0, 0, 0),
                        point(1, 0, 0),
                        point(1, 1, 0),
                        point(0, 1, 0),
                        point(0, 0, 1),
                        point(1, 0, 1),
                        point(1, 1, 1),
                        point(0, 1, 1));
        int[][] faces = {
            {0, 1, 2, 3}, {4, 7, 6, 5}, {0, 4, 5, 1}, {3, 2, 6, 7}, {0, 3, 7, 4}, {1, 5, 6, 2}
        };
        List<List<Double>> box = new ArrayList<>();
        for (int[] face : faces) for (int vertex : face) box.add(corners.get(vertex));
        if (boxCorners(box) == null || boxCorners(box).size() != 8)
            throw new AssertionError("native closed box not recognized");
        List<List<Double>> transformed = new ArrayList<>();
        for (List<Double> corner : box) {
            Vector3f vertex =
                    nativePart.transformPosition(
                            new Vector3f(
                                    corner.get(0).floatValue(),
                                    corner.get(1).floatValue(),
                                    corner.get(2).floatValue()));
            transformed.add(
                    point(
                            100 + (double) vertex.x,
                            50 + (double) vertex.y,
                            -70 + (double) vertex.z));
        }
        if (boxCorners(transformed) == null)
            throw new AssertionError("native rotated/scaled box not recognized");
        List<List<Double>> planar = new ArrayList<>();
        for (int left = 0; left < 4; left++)
            for (int right = left + 1; right < 4; right++) {
                planar.add(point(left, 0, 0));
                planar.add(point(right, 0, 0));
                planar.add(point(right, 1, 0));
                planar.add(point(left, 1, 0));
            }
        if (boxCorners(planar) != null)
            throw new AssertionError("overlapping planar quads accepted as a closed box");
        List<List<Double>> bent = new ArrayList<>(box);
        bent.set(0, point(0.2, 0, 0));
        if (boxCorners(bent) != null || !describe(bent).get("shape").equals("quads"))
            throw new AssertionError("non-box vertices were fitted to a box");
        if (boxCorners(box.subList(0, 20)) != null)
            throw new AssertionError("incomplete geometry accepted as a box");
        try {
            ((List<?>) describe(box).get("vertices")).clear();
            throw new AssertionError("mutable geometry receipt");
        } catch (UnsupportedOperationException expectedFailure) {
            // Receipt cannot be changed by a later frame.
        }
        receipt = Map.of("fresh", true);
        capturedAt = System.nanoTime();
        expire(null);
        if (receipt == null) throw new AssertionError("fresh geometry discarded");
        capturedAt = System.nanoTime() - RECEIPT_TTL - 1;
        expire(null);
        if (receipt != null || !unavailable.equals("stale_render"))
            throw new AssertionError("stale geometry retained");
        try {
            ((List<?>) ((List<?>) describe(box).get("vertices")).get(0)).clear();
            throw new AssertionError("mutable receipt vertex");
        } catch (UnsupportedOperationException expectedFailure) {
            // Vertex lists are immutable too.
        }
        try {
            describe(box).clear();
            throw new AssertionError("mutable receipt part");
        } catch (UnsupportedOperationException expectedFailure) {
            // Part metadata cannot be changed by callers.
        }
        selected = UUID.fromString("00000000-0000-0000-0000-000000000001");
        selectedWorld = new Object();
        selectionExpires = System.nanoTime() + SELECTION_TTL;
        receipt = Map.of("old", true);
        expire(new Object());
        if (selected != null || receipt != null)
            throw new AssertionError("world transition retained geometry");
        selected = UUID.fromString("00000000-0000-0000-0000-000000000001");
        selectedWorld = new Object();
        selectionExpires = System.nanoTime() - 1;
        receipt = Map.of("old", true);
        expire(selectedWorld);
        if (selected != null || receipt != null)
            throw new AssertionError("expired selection retained geometry");
        check(
                SCHEMA_V2.equals("blockfield.playergeometry.v2"),
                "receipt schema is not the v2 contract");
        check(excludedLayer("CapeLayer"), "cape layer must stay whitelisted");
        check(excludedLayer("ItemInHandLayer"), "held items must stay whitelisted");
        check(excludedLayer("PlayerItemInHandLayer"), "player held items must stay whitelisted");
        check(excludedLayer("ElytraLayer"), "elytra must stay whitelisted");
        check(excludedLayer("ArrowLayer"), "stuck arrows must stay whitelisted");
        check(excludedLayer("BeeStingerLayer"), "stingers must stay whitelisted");
        check(excludedLayer("CustomHeadLayer"), "worn heads must stay whitelisted");
        check(excludedLayer("ParrotOnShoulderLayer"), "shoulder parrots must stay whitelisted");
        check(excludedLayer("Deadmau5EarsLayer"), "deadmaus5 ears must stay whitelisted");
        check(excludedLayer("ParachuteRenderer"), "open parachute canopy must stay whitelisted");
        check(excludedLayer("class_972"), "intermediary cape layer must stay whitelisted");
        check(
                excludedLayer("class_4507"),
                "intermediary stuck-in-body layer must stay whitelisted");
        check(!excludedLayer("HumanoidArmorLayer"), "armor layers must be captured");
        check(!excludedLayer("AccessoriesRenderLayer"), "uniform layers must be captured");
        check(
                layerSource(new FakeUniformLayer()).equals("layer:FakeUniformLayer"),
                "layer source must name the rendering class");
        check(!beginLayer(null, new FakeUniformLayer()), "layer scope without a frame");
        endLayer(null, new FakeUniformLayer());
        copyLayerModel(new Object(), null);
        final ModelPart visor = plainPart(Map.of());
        final ModelPart headBone = plainPart(Map.of("visor", visor));
        Map<String, ModelPart> armorRoots = new LinkedHashMap<>();
        armorRoots.put("head", headBone);
        armorRoots.put("hat", plainPart(Map.of()));
        armorRoots.put("body", plainPart(Map.of()));
        armorRoots.put("right_arm", plainPart(Map.of()));
        armorRoots.put("left_arm", plainPart(Map.of()));
        armorRoots.put("right_leg", plainPart(Map.of()));
        armorRoots.put("left_leg", plainPart(Map.of()));
        Map<ModelPart, Owner> layerOwners = new IdentityHashMap<>();
        registerHumanoid(layerOwners, new HumanoidModel<>(plainPart(armorRoots)), "layer:X");
        check(
                new Owner("head", "head/visor", "layer:X").equals(layerOwners.get(visor)),
                "layer descendants must map to their bone with the child path");
        check(
                layerOwners.get(headBone).equals(new Owner("head", "head", "layer:X")),
                "layer bone roots must map to their own bone");
        check(
                layerOwners.size() == 8,
                "layer registration must cover six bones, hat and one child, got "
                        + layerOwners.size());
        Map<ModelPart, Owner> baseOwners = new IdentityHashMap<>();
        registerTree(baseOwners, headBone, "head", "head", BASE);
        check(
                baseOwners.get(visor).equals(new Owner("head", "head/visor", BASE)),
                "base descendants must keep the bone with the child path");
        check(childrenOf(headBone).get("visor") == visor, "child lookup must see real parts");
        BoxRun cubes = new BoxRun(new Owner("head", "head", BASE));
        cubes.vertices.addAll(box);
        List<List<Double>> shifted = new ArrayList<>();
        for (List<Double> vertex : box)
            shifted.add(point(vertex.get(0) + 5, vertex.get(1), vertex.get(2)));
        cubes.vertices.addAll(shifted);
        List<Map<String, Object>> entries = boxEntries(cubes);
        check(entries.size() == 2, "two closed cubes must split into two boxes");
        for (Map<String, Object> boxEntry : entries) {
            check(boxEntry.get("shape").equals("box"), "split entries must stay boxes");
            check(((List<?>) boxEntry.get("vertices")).size() == 24, "split boxes hold 24 runs");
            check(boxEntry.get("id").equals("head"), "split entries keep the part id");
            check(boxEntry.get("source").equals(BASE), "split entries keep the source");
        }
        BoxRun open = new BoxRun(new Owner("torso", "torso", "layer:Y"));
        open.vertices.addAll(bent);
        List<Map<String, Object>> quads = boxEntries(open);
        check(
                quads.size() == 1 && quads.get(0).get("shape").equals("quads"),
                "open runs must stay a single quads entry");
        BoxRun empty = new BoxRun(new Owner("head", "head", BASE));
        check(boxEntries(empty).isEmpty(), "empty runs must emit no boxes");
        // The Blockfield renderer class is absent from the testbot runtime, so the F3+B
        // snapshot stays null and receipts carry no debug boxes here; in game the same
        // call returns the drawn boxes §9.1(10) compares with the server dump.
        check(debugSnapshot(7) == null, "debug snapshot must be null without the mod renderer");
        Map<String, Object> drawn =
                Map.of("tick", 5L, "serverTick", 790L, "shapeKey", "wide/7f/none");
        Map<String, Object> recorded = debugBoxesForReceipt(new LinkedHashMap<>(drawn));
        check(recorded.get("serverTick").equals(790L), "receipt must keep the drawn server tick");
        check(recorded.get("tick").equals(5L), "receipt must keep the client draw tick");
        try {
            recorded.clear();
            throw new AssertionError("mutable debug boxes copy");
        } catch (UnsupportedOperationException expectedFailure) {
            // The receipt copy must remain a snapshot after the renderer draws the next frame.
        }
        System.out.println("RenderedPlayerGeometry self-check passed");
    }

    private static final class FakeUniformLayer {}
}
