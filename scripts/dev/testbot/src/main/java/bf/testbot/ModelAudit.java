package bf.testbot;

import com.mojang.blaze3d.platform.Lighting;
import com.mojang.blaze3d.vertex.PoseStack;
import com.mojang.logging.LogUtils;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.client.renderer.texture.MissingTextureAtlasSprite;
import net.minecraft.client.resources.model.BakedModel;
import net.minecraft.core.Direction;
import net.minecraft.core.RegistryAccess;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.util.RandomSource;
import net.minecraft.world.item.CreativeModeTabs;
import net.minecraft.world.item.ItemStack;

import org.slf4j.Logger;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class ModelAudit {
    private static final Logger LOGGER = LogUtils.getLogger();

    private ModelAudit() {}

    private static List<ItemStack> items(Minecraft mc, String namespace) {
        LocalPlayer player = mc.player;
        if (player == null || mc.level == null) throw new IllegalStateException("not in a world");
        if (!namespace.equals("all") && !ResourceLocation.isValidNamespace(namespace))
            throw new IllegalArgumentException("expected namespace or all");
        // Same rebuild as CreativeModeInventoryScreen: keyed on this session's registries, so a
        // reconnect never reuses another server's contents and the inventory search stays in sync.
        RegistryAccess registries = mc.level.registryAccess();
        if (CreativeModeTabs.tryRebuildTabContents(
                player.connection.enabledFeatures(),
                player.canUseGameMasterBlocks() && mc.options.operatorItemsTab().get(),
                registries)) {
            List<ItemStack> search = List.copyOf(CreativeModeTabs.searchTab().getDisplayItems());
            player.connection.searchTrees().updateCreativeTooltips(registries, search);
            player.connection.searchTrees().updateCreativeTags(search);
        }
        List<ItemStack> all = List.copyOf(CreativeModeTabs.searchTab().getDisplayItems());
        return namespace.equals("all")
                ? all
                : all.stream()
                        .filter(
                                stack ->
                                        BuiltInRegistries.ITEM
                                                .getKey(stack.getItem())
                                                .getNamespace()
                                                .equals(namespace))
                        .toList();
    }

    private static int offset(String value, int total) {
        int offset = Integer.parseInt(value);
        if (offset < 0 || offset > total)
            throw new IllegalArgumentException("offset must be between 0 and " + total);
        return offset;
    }

    private static Map<String, Object> bounds(String namespace, int total, int offset, int end) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("namespace", namespace);
        result.put("total", total);
        result.put("offset", offset);
        result.put("endOffset", end);
        result.put("count", end - offset);
        result.put("nextOffset", end < total ? end : null);
        return result;
    }

    private static Map<String, Object> describe(ItemStack stack, int index) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("index", index);
        result.put("id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
        try {
            result.put("name", stack.getHoverName().getString());
        } catch (RuntimeException | Error e) {
            result.put("nameError", e.toString());
        }
        return result;
    }

    static Object audit(Minecraft mc, String[] args) {
        if (args.length != 4)
            throw new IllegalArgumentException("modelaudit <namespace|all> <offset> <limit>");
        int limit = Integer.parseInt(args[3]);
        if (limit < 1 || limit > 128)
            throw new IllegalArgumentException("limit must be between 1 and 128");
        List<ItemStack> items = items(mc, args[1]);
        int offset = offset(args[2], items.size());
        int end = offset + Math.min(limit, items.size() - offset);
        List<Map<String, Object>> candidates = new ArrayList<>();
        for (int index = offset; index < end; index++) {
            ItemStack stack = items.get(index);
            Map<String, Object> candidate = describe(stack, index);
            try {
                BakedModel model = mc.getItemRenderer().getModel(stack, mc.level, mc.player, 0);
                boolean missingModel = model == mc.getModelManager().getMissingModel();
                boolean customRenderer = model.isCustomRenderer();
                boolean missingSprite = missingSprite(model, null);
                for (Direction direction : Direction.values())
                    missingSprite |= missingSprite(model, direction);
                if (!missingModel
                        && !customRenderer
                        && !missingSprite
                        && !candidate.containsKey("nameError")) continue;
                candidate.put("missingModel", missingModel);
                candidate.put("customRenderer", customRenderer);
                candidate.put("missingSprite", missingSprite);
            } catch (RuntimeException | Error e) {
                candidate.put("error", e.toString());
            }
            candidates.add(candidate);
        }
        Map<String, Object> result = bounds(args[1], items.size(), offset, end);
        result.put("candidates", candidates);
        result.put(
                "note",
                "Missing sprites can be hidden internal faces; custom renderers need visual"
                        + " checking.");
        return result;
    }

    private static boolean missingSprite(BakedModel model, Direction direction) {
        return model.getQuads(null, direction, RandomSource.create(42)).stream()
                .anyMatch(
                        quad ->
                                quad.getSprite()
                                        .contents()
                                        .name()
                                        .equals(MissingTextureAtlasSprite.getLocation()));
    }

    static Object tabs(Minecraft mc, String[] args) {
        if (args.length != 1) throw new IllegalArgumentException("creativeaudit");
        items(mc, "all"); // rebuilds every tab for this session
        List<Map<String, Object>> tabs = new ArrayList<>();
        for (var tab : CreativeModeTabs.allTabs()) {
            ResourceLocation id = BuiltInRegistries.CREATIVE_MODE_TAB.getKey(tab);
            Collection<ItemStack> displayed = tab.getDisplayItems();
            if (!id.getNamespace().equals("minecraft") || displayed.isEmpty()) continue;
            var foreignItems = new LinkedHashSet<String>();
            for (ItemStack stack : displayed) {
                ResourceLocation itemId = BuiltInRegistries.ITEM.getKey(stack.getItem());
                if (!itemId.getNamespace().equals("minecraft")) foreignItems.add(itemId.toString());
            }
            tabs.add(
                    Map.of(
                            "id", id.toString(),
                            "title", tab.getDisplayName().getString(),
                            "type", tab.getType().name(),
                            "total", displayed.size(),
                            "foreignCount", foreignItems.size(),
                            "foreignItems", List.copyOf(foreignItems)));
        }
        return Map.of("tabs", tabs);
    }

    static Object page(Minecraft mc, String[] args) {
        if (args.length != 3)
            throw new IllegalArgumentException("modelpage <namespace|all> <offset>");
        List<ItemStack> items = items(mc, args[1]);
        int offset = offset(args[2], items.size());
        if (offset == items.size())
            throw new IllegalArgumentException("no creative items at this offset for " + args[1]);
        if (mc.getWindow().getGuiScaledWidth() < 64 || mc.getWindow().getGuiScaledHeight() < 92)
            throw new IllegalStateException("GUI too small for an item page");
        Page page = new Page(args[1], items, offset);
        page.init(mc, mc.getWindow().getGuiScaledWidth(), mc.getWindow().getGuiScaledHeight());
        Map<String, Object> result = bounds(args[1], items.size(), offset, page.end);
        List<Map<String, Object>> legend = new ArrayList<>();
        for (int index = offset; index < page.end; index++)
            legend.add(describe(items.get(index), index));
        result.put("columns", page.columns);
        result.put("items", legend);
        mc.setScreen(page);
        return result;
    }

    static void renderIsolated(PoseStack pose, Runnable render) {
        pose.pushPose();
        PoseStack.Pose mark = pose.last();
        try {
            render.run();
        } finally {
            // ItemRenderer and custom renderers can leave more than one pushed pose on failure.
            while (pose.last() != mark) pose.popPose();
            pose.popPose();
        }
    }

    public static void main(String[] args) {
        PoseStack pose = new PoseStack();
        PoseStack.Pose base = pose.last();
        RuntimeException failure = new IllegalStateException("broken item renderer");
        for (int depth : new int[] {0, 2}) {
            try {
                renderIsolated(
                        pose,
                        () -> {
                            for (int n = 0; n < depth; n++) {
                                pose.pushPose();
                                pose.translate(8, 8, 150);
                                pose.scale(16, -16, 16);
                            }
                            throw failure;
                        });
                throw new AssertionError("renderer failure was swallowed");
            } catch (RuntimeException caught) {
                if (caught != failure) throw new AssertionError("wrong renderer failure", caught);
            }
            if (pose.last() != base || !pose.clear())
                throw new AssertionError("failed renderer corrupted the screen pose");
        }
        renderIsolated(pose, () -> pose.translate(1, 2, 3));
        if (pose.last() != base || !pose.clear())
            throw new AssertionError("successful renderer corrupted the screen pose");
    }

    private static final class Page extends Screen {
        private static final int CELL_WIDTH = 40;
        private static final int CELL_HEIGHT = 36;
        private final List<ItemStack> items;
        private final int offset;
        private final Set<Integer> failed = new HashSet<>();
        private int columns;
        private int end;

        private Page(String namespace, List<ItemStack> items, int offset) {
            super(Component.literal("Creative item models: " + namespace));
            this.items = items;
            this.offset = offset;
        }

        @Override
        protected void init() {
            columns = Math.max(1, Math.min(12, (width - 24) / CELL_WIDTH));
            int rows = Math.max(1, Math.min(6, (height - 56) / CELL_HEIGHT));
            end = offset + Math.min(columns * rows, items.size() - offset);
        }

        @Override
        public void render(GuiGraphics graphics, int mouseX, int mouseY, float partialTick) {
            graphics.fill(0, 0, width, height, 0xFF30343A);
            graphics.drawString(font, title, 12, 10, 0xFFFFFFFF, false);
            graphics.drawString(
                    font,
                    offset + ".." + (end - 1) + " / " + items.size() + "  |  Esc: close",
                    12,
                    23,
                    0xFFD8D8D8,
                    false);
            int left = (width - columns * CELL_WIDTH) / 2;
            for (int index = offset; index < end; index++) {
                ItemStack stack = items.get(index);
                int cell = index - offset;
                int x = left + cell % columns * CELL_WIDTH;
                int y = 40 + cell / columns * CELL_HEIGHT;
                graphics.fill(x + 1, y, x + CELL_WIDTH - 1, y + CELL_HEIGHT - 2, 0xFF484C52);
                if (!failed.contains(index)) {
                    try {
                        renderIsolated(
                                graphics.pose(),
                                () -> graphics.renderItem(stack, x + (CELL_WIDTH - 16) / 2, y + 3));
                    } catch (RuntimeException | Error e) {
                        Lighting.setupFor3DItems();
                        failed.add(index);
                        LOGGER.warn(
                                "modelpage: item {} {} failed to render",
                                index,
                                BuiltInRegistries.ITEM.getKey(stack.getItem()),
                                e);
                    }
                }
                if (failed.contains(index))
                    graphics.drawCenteredString(font, "ERR", x + CELL_WIDTH / 2, y + 7, 0xFFFF5555);
                graphics.drawCenteredString(
                        font, Integer.toString(index), x + CELL_WIDTH / 2, y + 23, 0xFFFFFFFF);
            }
        }

        @Override
        public boolean isPauseScreen() {
            return false;
        }
    }
}
