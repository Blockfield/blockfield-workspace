package bf.testbot;

import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.world.entity.player.Player;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * "attempt grab|gap|crawl &lt;yaw&gt;": one ParCool move from where the server put the player,
 * played tick by tick through the real key mappings, so ParCool sees it as a player's input.
 * "attempt get": progress and result.
 *
 * <ul>
 *   <li>grab: face the wall, step to the edge of the start block, hold ClingToCliff, jump; once
 *       clinging, jump again (ClimbUp) and walk on;
 *   <li>gap: back off up to 3 blocks, sprint forward, jump at the edge of the start block, then as
 *       grab;
 *   <li>crawl: hold Crawl and forward.
 * </ul>
 *
 * The result lists every block stood on ("x z y"), so the caller decides whether the target was
 * reached.
 */
final class Parkour {
    private static final int TICKS = 90;
    private static final List<String> KINDS = List.of("grab", "gap", "crawl");
    private static Run run;

    private static final class Run {
        final String kind;
        final float yaw;
        final double sx, sz, dx, dz;
        int t, clingAt = -1, climbAt = -1, crawlTicks;
        boolean running, jumped;
        double lastAlong, maxY = -1e9;
        final Set<String> ground = new LinkedHashSet<>();
        boolean done;

        Run(String kind, float yaw, LocalPlayer p) {
            this.kind = kind;
            this.yaw = yaw;
            this.sx = p.getX();
            this.sz = p.getZ();
            this.dx = -Math.sin(Math.toRadians(yaw));
            this.dz = Math.cos(Math.toRadians(yaw));
        }
    }

    private Parkour() {}

    static Object start(String kind, float yaw) {
        if (!KINDS.contains(kind))
            throw new IllegalArgumentException("attempt grab|gap|crawl <yaw> | attempt get");
        LocalPlayer p = Minecraft.getInstance().player;
        if (p == null) throw new IllegalStateException("not in a world");
        run = new Run(kind, yaw, p);
        return Map.of("ok", true, "ticks", TICKS);
    }

    static Object get() {
        Run r = run;
        if (r == null) throw new IllegalStateException("no attempt");
        LocalPlayer p = Minecraft.getInstance().player;
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("done", r.done);
        m.put("t", r.t);
        m.put("clingAt", r.clingAt);
        m.put("climbAt", r.climbAt);
        m.put("crawlTicks", r.crawlTicks);
        m.put("maxY", Math.round(r.maxY * 1000) / 1000.0);
        m.put("ground", List.copyOf(r.ground));
        if (p != null) m.put("pos", List.of(p.getX(), p.getY(), p.getZ()));
        return m;
    }

    static void tick(Minecraft mc) {
        Run r = run;
        LocalPlayer p = mc.player;
        if (r == null || r.done || p == null) return;
        p.setYRot(r.yaw);
        p.setYHeadRot(r.yaw);
        p.setXRot(0);
        Options o = mc.options;
        if (doing(p, "ClingToCliff") && r.clingAt < 0) r.clingAt = r.t;
        if (doing(p, "ClimbUp") && r.climbAt < 0) r.climbAt = r.t;
        if (doing(p, "Crawl")) r.crawlTicks++;
        double along = (p.getX() - r.sx) * r.dx + (p.getZ() - r.sz) * r.dz;
        switch (r.kind) {
            case "grab" -> {
                // A player stops at the edge before jumping: walking on would go under an overhang
                // or off a drop.
                if (along < 0.2 || r.clingAt >= 0) TestBot.hold(o.keyUp, 1);
                TestBot.hold(TestBot.mappingOf(mc, "key.parcool.ClingToCliff"), 2);
                if (r.t == 2) TestBot.hold(o.keyJump, 2);
            }
            case "gap" -> {
                TestBot.hold(TestBot.mappingOf(mc, "key.parcool.ClingToCliff"), 2);
                if (!r.running) {
                    // Run-up: back off until 3 blocks behind the start, a wall or 40 ticks.
                    TestBot.hold(o.keyDown, 2);
                    r.running =
                            along <= -3
                                    || r.t > 3 && Math.abs(along - r.lastAlong) < 0.01
                                    || r.t > 40;
                    r.lastAlong = along;
                } else {
                    TestBot.hold(o.keyUp, 2);
                    TestBot.hold(o.keySprint, 2);
                    if (!r.jumped && p.onGround() && along >= 0.35) {
                        TestBot.hold(o.keyJump, 2);
                        r.jumped = true;
                    }
                }
            }
            default -> {
                TestBot.hold(o.keyUp, 2);
                TestBot.hold(TestBot.mappingOf(mc, "key.parcool.Crawl"), 2);
            }
        }
        // ClimbUp needs more than 2 ticks of clinging and a fresh jump press.
        if (r.clingAt >= 0 && r.t == r.clingAt + 5) TestBot.hold(o.keyJump, 2);
        if (p.onGround())
            r.ground.add(
                    p.getBlockX()
                            + " "
                            + p.getBlockZ()
                            + " "
                            + Math.round(p.getY() * 1000) / 1000.0);
        r.maxY = Math.max(r.maxY, p.getY());
        if (++r.t >= TICKS) r.done = true;
    }

    /** ParCool action state by reflection: the bot does not depend on ParCool. */
    private static boolean doing(Player p, String action) {
        try {
            Object parkourability =
                    Class.forName("com.alrex.parcool.common.attachment.common.Parkourability")
                            .getMethod("get", Player.class)
                            .invoke(null, p);
            if (parkourability == null) return false;
            Class<?> type = Class.forName("com.alrex.parcool.common.action.impl." + action);
            Object a =
                    parkourability
                            .getClass()
                            .getMethod("get", Class.class)
                            .invoke(parkourability, type);
            return (boolean) a.getClass().getMethod("isDoing").invoke(a);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return false;
        }
    }
}
