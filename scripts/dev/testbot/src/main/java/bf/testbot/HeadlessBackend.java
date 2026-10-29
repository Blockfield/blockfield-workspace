package bf.testbot;

import com.sun.jna.Memory;
import com.sun.jna.Native;
import com.sun.jna.Platform;
import com.sun.jna.Pointer;
import com.sun.jna.platform.win32.Kernel32;
import com.sun.jna.ptr.IntByReference;
import com.sun.jna.win32.StdCallLibrary;

import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.gui.screens.options.OptionsScreen;

import org.lwjgl.opengl.GL11;
import org.lwjgl.opengl.GL20;

import java.util.LinkedHashMap;
import java.util.Map;

/** Reports only this client's GL context and its explicitly assigned isolated backend. */
public final class HeadlessBackend {
    public static volatile boolean voiceChatBlocked;

    private HeadlessBackend() {}

    public static String screenKind(Screen screen) {
        if (screen == null) return null;
        if (screen instanceof TitleScreen) return "TitleScreen";
        if (screen instanceof OptionsScreen) return "OptionsScreen";
        return screen.getClass().getSimpleName();
    }

    public interface UserObjects extends StdCallLibrary {
        Pointer GetProcessWindowStation();

        Pointer GetThreadDesktop(int thread);

        boolean GetUserObjectInformationW(
                Pointer object, int index, Pointer output, int size, IntByReference needed);
    }

    public static Map<String, Object> describe() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put(
                "platform",
                Platform.isWindows() ? "win32" : Platform.isLinux() ? "linux" : "other");
        result.put("pid", ProcessHandle.current().pid());
        result.put("renderThread", Thread.currentThread().getName());
        result.put("renderer", glString(GL11.GL_RENDERER));
        result.put("version", glString(GL11.GL_VERSION));
        result.put("vendor", glString(GL11.GL_VENDOR));
        result.put("glsl", glString(GL20.GL_SHADING_LANGUAGE_VERSION));
        result.put("silent", "null".equals(System.getenv("ALSOFT_DRIVERS")));
        result.put("voiceChatBlocked", voiceChatBlocked);
        if (Platform.isWindows()) {
            String station = System.getenv("BF_HEADLESS_STATION");
            String desktop = System.getenv("BF_HEADLESS_DESKTOP");
            String owner = System.getenv("BF_HEADLESS_OWNER");
            if (station == null && desktop == null && owner == null) {
                result.put("verified", false);
                return result;
            }
            if (station == null
                    || !station.startsWith("Blockfield-")
                    || desktop == null
                    || !desktop.startsWith("Blockfield-")
                    || owner == null
                    || owner.isEmpty()) {
                throw new IllegalStateException("isolated Windows backend receipt is missing");
            }
            UserObjects api = Native.load("user32", UserObjects.class);
            Pointer stationHandle = require(api.GetProcessWindowStation());
            Pointer desktopHandle =
                    require(api.GetThreadDesktop(Kernel32.INSTANCE.GetCurrentThreadId()));
            String actualStation = objectName(api, stationHandle);
            String actualDesktop = objectName(api, desktopHandle);
            boolean visible;
            try (Memory flags = new Memory(12)) {
                if (!api.GetUserObjectInformationW(
                        stationHandle, 1, flags, 12, new IntByReference())) {
                    throw new IllegalStateException(
                            "cannot query isolated station flags: " + Native.getLastError());
                }
                visible = (flags.getInt(8) & 1) != 0;
            }
            if (!station.equals(actualStation) || !desktop.equals(actualDesktop) || visible) {
                throw new IllegalStateException(
                        "render thread is not on its assigned noninteractive Windows backend");
            }
            result.put("station", actualStation);
            result.put("desktop", actualDesktop);
            result.put("visible", visible);
            result.put("owner", owner);
            result.put("verified", true);
        } else if (Platform.isLinux()) {
            result.put("display", System.getenv("DISPLAY"));
            result.put("authority", System.getenv("XAUTHORITY"));
            result.put("wayland", System.getenv("WAYLAND_DISPLAY"));
        }
        return result;
    }

    private static Pointer require(Pointer handle) {
        if (handle == null || Pointer.nativeValue(handle) == 0) {
            throw new IllegalStateException(
                    "cannot query this client's backend: " + Native.getLastError());
        }
        return handle;
    }

    private static String objectName(UserObjects api, Pointer handle) {
        try (Memory name = new Memory(512)) {
            if (!api.GetUserObjectInformationW(handle, 2, name, 512, new IntByReference())) {
                throw new IllegalStateException(
                        "cannot query isolated backend name: " + Native.getLastError());
            }
            return name.getWideString(0);
        }
    }

    private static String glString(int name) {
        String value = GL11.glGetString(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalStateException("render thread has no active OpenGL context");
        }
        return value;
    }
}
