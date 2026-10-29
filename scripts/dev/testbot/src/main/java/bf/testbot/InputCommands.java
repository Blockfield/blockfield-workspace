package bf.testbot;

import bf.testbot.mixin.MinecraftInputAccess;
import bf.testbot.mixin.MouseHandlerInvoker;

import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

import java.util.Locale;
import java.util.Map;

/** Vanilla input entry points, independent of which mod owns a physical mouse binding. */
final class InputCommands {
    private InputCommands() {}

    static Object scroll(Minecraft mc, String[] args) {
        double[] offsets = scrollOffsets(args);
        ((MouseHandlerInvoker) mc.mouseHandler)
                .bf$onScroll(mc.getWindow().getWindow(), offsets[1], offsets[0]);
        return Map.of("scrolled", true, "dy", offsets[0], "dx", offsets[1]);
    }

    static Object use(Minecraft mc, String[] args) {
        int ticks = useTicks(args);
        requireGameplay(mc);
        if (mc.hitResult == null) throw new IllegalStateException("no crosshair hit result yet");
        // Hold the actual binding: a physical RMB can belong to another mod or be rebound.
        TestBot.hold(mc.options.keyUse, ticks);
        boolean started = !mc.player.isUsingItem();
        if (started) ((MinecraftInputAccess) mc).bf$startUseItem();
        return Map.of(
                "pressed", mc.options.keyUse.saveString(), "ticks", ticks, "startUseItem", started);
    }

    static Object useOn(Minecraft mc, String[] args) {
        if (args.length != 5) throw new IllegalArgumentException("use_on <x> <y> <z> <face>");
        BlockPos pos =
                new BlockPos(
                        Integer.parseInt(args[1]),
                        Integer.parseInt(args[2]),
                        Integer.parseInt(args[3]));
        Direction face = Direction.valueOf(args[4].toUpperCase(Locale.ROOT));
        requireGameplay(mc);
        if (!mc.level.hasChunkAt(pos)
                || !mc.level.getWorldBorder().isWithinBounds(pos)
                || mc.level.getBlockState(pos).isAir()
                || !mc.player.canInteractWithBlock(pos, 0))
            throw new IllegalArgumentException(
                    "target must be a loaded block within reach and border");
        Vec3 at =
                Vec3.atCenterOf(pos)
                        .add(face.getStepX() * 0.5, face.getStepY() * 0.5, face.getStepZ() * 0.5);
        HitResult previous = mc.hitResult;
        mc.hitResult = new BlockHitResult(at, face, pos, false);
        try {
            return use(mc, new String[] {"use"});
        } finally {
            mc.hitResult = previous;
        }
    }

    static Object sneak(Minecraft mc, String[] args) {
        if (args.length != 2 || !(args[1].equals("on") || args[1].equals("off")))
            throw new IllegalArgumentException("sneak on|off");
        requireGameplay(mc);
        boolean down = args[1].equals("on");
        if (mc.options.keyShift.isDown() != down)
            mc.options.keyShift.setDown(down || mc.options.toggleCrouch().get());
        // Commands can arrive before the next movement tick; callbacks must already see sneak.
        mc.player.input.shiftKeyDown = down;
        mc.player.setShiftKeyDown(down);
        return Map.of("sneaking", down);
    }

    private static void requireGameplay(Minecraft mc) {
        if (mc.player == null || mc.level == null || mc.gameMode == null)
            throw new IllegalStateException("not in a world");
        if (mc.screen != null || mc.getOverlay() != null)
            throw new IllegalStateException("close the screen/overlay before gameplay input");
    }

    private static double[] scrollOffsets(String[] args) {
        if (args.length < 2 || args.length > 3)
            throw new IllegalArgumentException("scroll <dy> [dx]");
        double dy = Double.parseDouble(args[1]);
        double dx = args.length == 3 ? Double.parseDouble(args[2]) : 0;
        if (!Double.isFinite(dy) || !Double.isFinite(dx))
            throw new IllegalArgumentException("scroll offsets must be finite");
        return new double[] {dy, dx};
    }

    private static int useTicks(String[] args) {
        if (args.length < 1 || args.length > 2) throw new IllegalArgumentException("use [ticks]");
        int ticks = args.length == 2 ? Integer.parseInt(args[1]) : 2;
        if (ticks < 1 || ticks > RawInputState.HOLD_TICKS)
            throw new IllegalArgumentException("use ticks must be 1..200");
        return ticks;
    }

    public static void main(String[] args) {
        double[] offsets = scrollOffsets(new String[] {"scroll", "0.25", "-1.5"});
        if (offsets[0] != 0.25
                || offsets[1] != -1.5
                || scrollOffsets(new String[] {"scroll", "4"})[1] != 0
                || useTicks(new String[] {"use"}) != 2
                || useTicks(new String[] {"use", "200"}) != 200)
            throw new AssertionError("input command parsing failed");
        for (String invalid :
                new String[] {
                    "scroll",
                    "scroll NaN",
                    "scroll 1 Infinity",
                    "scroll 1 2 3",
                    "use 0",
                    "use -1",
                    "use 201",
                    "use 0.25",
                    "use 2 3"
                }) {
            try {
                String[] parts = invalid.split(" ");
                if (parts[0].equals("scroll")) scrollOffsets(parts);
                else useTicks(parts);
                throw new AssertionError("invalid input accepted: " + invalid);
            } catch (IllegalArgumentException expected) {
            }
        }
        System.out.println("PASS fractional scroll axes, use duration and invalid input rejection");
    }
}
