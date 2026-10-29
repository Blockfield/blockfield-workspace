package bf.testbot;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Synthetic polling leases belong to one client window, never to the desktop. */
public final class RawInputState {
    public static final int HOLD_TICKS = 200;
    public static final long LEASE_NANOS = 10_000_000_000L;

    public record Key(boolean mouse, int code) {}

    private record Hold(int tick, long nanos) {}

    private final Map<Key, Hold> held = new HashMap<>();
    private long window;
    private long deadline;
    private int x;
    private int y;
    private boolean cursor;

    public synchronized void activate(long window, long now) {
        if (window == 0) throw new IllegalArgumentException("no client window");
        if (this.window != 0 && this.window != window)
            throw new IllegalStateException("reset before changing client window");
        this.window = window;
        deadline = now + LEASE_NANOS;
    }

    public synchronized boolean active(long window, long now) {
        return window != 0 && this.window == window && now < deadline;
    }

    public synchronized boolean hold(Key key, int ticks, int tick, long now) {
        if (ticks < 1 || ticks > HOLD_TICKS || key.code < 0 || key.code > (key.mouse ? 2 : 348))
            throw new IllegalArgumentException("invalid synthetic input");
        Hold previous = held.put(key, new Hold(tick + ticks, now + LEASE_NANOS));
        return previous == null || previous.tick <= tick || previous.nanos <= now;
    }

    public synchronized void release(Key key) {
        held.remove(key);
    }

    public synchronized Boolean pressed(long window, Key key, int tick, long now) {
        Hold hold = held.get(key);
        return active(window, now) && hold != null && tick < hold.tick && now < hold.nanos
                ? Boolean.TRUE
                : null;
    }

    public synchronized List<Key> expire(int tick, long now) {
        List<Key> releases = new ArrayList<>();
        held.entrySet()
                .removeIf(
                        entry -> {
                            Hold hold = entry.getValue();
                            if (tick >= hold.tick || now >= hold.nanos || now >= deadline) {
                                releases.add(entry.getKey());
                                return true;
                            }
                            return false;
                        });
        return releases;
    }

    public synchronized void move(int x, int y, int width, int height) {
        if (x < 0 || y < 0 || x >= width || y >= height || width > 32768 || height > 32768)
            throw new IllegalArgumentException("coordinates must be inside the client window");
        this.x = x;
        this.y = y;
        cursor = true;
    }

    public synchronized int[] cursor(long window, long now) {
        return active(window, now) && cursor ? new int[] {x, y} : null;
    }

    public synchronized List<Key> reset() {
        List<Key> releases = new ArrayList<>(held.keySet());
        held.clear();
        window = 0;
        deadline = 0;
        cursor = false;
        return releases;
    }

    public static String keyName(String value) {
        if (!value.matches("[A-Za-z0-9_]{1,16}"))
            throw new IllegalArgumentException("invalid raw key");
        String key =
                switch (value.toLowerCase(java.util.Locale.ROOT)) {
                    case "return", "enter" -> "enter";
                    case "esc", "escape" -> "escape";
                    case "shift_l" -> "left.shift";
                    case "shift_r" -> "right.shift";
                    case "control_l", "ctrl_l" -> "left.control";
                    case "control_r", "ctrl_r" -> "right.control";
                    case "alt_l" -> "left.alt";
                    case "alt_r" -> "right.alt";
                    case "super_l" -> "left.win";
                    case "super_r" -> "right.win";
                    case "prior", "pageup" -> "page.up";
                    case "next", "pagedown" -> "page.down";
                    case "print", "printscreen" -> "print.screen";
                    case "bracketleft" -> "left.bracket";
                    case "bracketright" -> "right.bracket";
                    case "grave" -> "grave.accent";
                    default -> {
                        String lower = value.toLowerCase(java.util.Locale.ROOT);
                        yield lower.startsWith("kp_")
                                ? "keypad." + lower.substring(3)
                                : lower.replace('_', '.');
                    }
                };
        return "key.keyboard." + key;
    }

    /** {@code F3+T}-style combination: 2–4 distinct keys, pressed in order. */
    public static List<String> comboNames(String value) {
        String[] parts = value.split("\\+", -1);
        if (parts.length < 2 || parts.length > 4)
            throw new IllegalArgumentException("a combo needs 2-4 keys joined by +");
        List<String> names = new ArrayList<>();
        for (String part : parts) {
            String name = keyName(part);
            if (names.contains(name)) throw new IllegalArgumentException("repeated combo key");
            names.add(name);
        }
        return names;
    }

    /** GLFW modifier bit a held key contributes to later presses (shift, control, alt, super). */
    public static int modifier(int code) {
        return switch (code) {
            case 340, 344 -> 1;
            case 341, 345 -> 2;
            case 342, 346 -> 4;
            case 343, 347 -> 8;
            default -> 0;
        };
    }

    public static int mouseCode(int button) {
        // Public X11 buttons 1/2/3 mean left/middle/right; GLFW assigns 0/2/1.
        return switch (button) {
            case 1 -> 0;
            case 2 -> 2;
            case 3 -> 1;
            default -> throw new IllegalArgumentException("button must be 1..3");
        };
    }

    public static void main(String[] args) {
        RawInputState state = new RawInputState();
        Key button = new Key(true, 1);
        state.activate(7, 0);
        check(state.hold(button, HOLD_TICKS, 0, 0));
        check(state.pressed(7, button, 1, 1) == Boolean.TRUE);
        check(state.pressed(8, button, 1, 1) == null);
        check(!state.hold(button, HOLD_TICKS, 1, 1));
        check(state.expire(201, 2).equals(List.of(button)));
        state.hold(button, HOLD_TICKS, 0, 0);
        check(state.expire(1, LEASE_NANOS).equals(List.of(button)));
        state.activate(7, LEASE_NANOS);
        state.hold(button, 2, 0, LEASE_NANOS);
        check(state.expire(1, LEASE_NANOS + 1).isEmpty());
        check(state.expire(2, LEASE_NANOS + 2).equals(List.of(button)));
        state.move(639, 359, 640, 360);
        check(state.cursor(7, LEASE_NANOS + 1)[0] == 639);
        try {
            state.move(640, 0, 640, 360);
            throw new AssertionError("window bounds were not enforced");
        } catch (IllegalArgumentException expected) {
        }
        state.hold(button, HOLD_TICKS, 0, LEASE_NANOS);
        check(state.reset().equals(List.of(button)));
        check(!state.active(7, LEASE_NANOS + 1));
        check(state.cursor(7, LEASE_NANOS + 1) == null);
        check(keyName("Escape").equals("key.keyboard.escape"));
        check(keyName("Shift_L").equals("key.keyboard.left.shift"));
        check(keyName("KP_Enter").equals("key.keyboard.keypad.enter"));
        check(keyName("Caps_Lock").equals("key.keyboard.caps.lock"));
        check(mouseCode(1) == 0 && mouseCode(2) == 2 && mouseCode(3) == 1);
        check(comboNames("F3+T").equals(List.of("key.keyboard.f3", "key.keyboard.t")));
        check(comboNames("Control_L+Shift_L+T").get(1).equals("key.keyboard.left.shift"));
        for (String invalid : new String[] {"F3", "F3+", "F3+F3", "a+b+c+d+e", "F3+T;stop"})
            try {
                comboNames(invalid);
                throw new AssertionError("invalid combo accepted: " + invalid);
            } catch (IllegalArgumentException expected) {
            }
        check(modifier(341) == 2 && modifier(344) == 1 && modifier(292) == 0);
        System.out.println(
                "PASS raw input ownership, renewal, expiry, reset, window bounds and combos");
    }

    private static void check(boolean valid) {
        if (!valid) throw new AssertionError("raw input state check failed");
    }
}
