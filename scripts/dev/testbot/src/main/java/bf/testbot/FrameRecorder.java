package bf.testbot;

import com.mojang.blaze3d.pipeline.RenderTarget;
import com.mojang.blaze3d.platform.GlStateManager;

import net.minecraft.client.Minecraft;

import org.lwjgl.opengl.GL11;
import org.lwjgl.opengl.GL30;
import org.lwjgl.system.MemoryUtil;

import java.io.BufferedOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.zip.Deflater;

/**
 * Records every frame this client presents, with its own timestamp, so animation smoothness can be
 * judged at the real frame rate: {@code record start <name> <seconds>} / {@code record status}. The
 * file holds deflated bottom-up RGB frames; {@code scripts/dev/record_video.py} encodes it.
 */
public final class FrameRecorder {
    private static final int MAX_SECONDS = 20;
    private static final int POOL = 12;
    private static volatile Session session;
    private static Session last;

    private FrameRecorder() {}

    static Map<String, Object> start(Path directory, String name, double seconds) {
        if (!name.matches("[a-z0-9_-]{1,40}"))
            throw new IllegalArgumentException("record name must match [a-z0-9_-]{1,40}");
        if (!(seconds > 0 && seconds <= MAX_SECONDS))
            throw new IllegalArgumentException("record seconds must be in (0, 20]");
        if (session != null) throw new IllegalStateException("a recording is already running");
        RenderTarget target = Minecraft.getInstance().getMainRenderTarget();
        Path file = directory.resolve(name + ".bfrec");
        Session next;
        try {
            Files.createDirectories(directory);
            next = new Session(file, target.width, target.height, seconds);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        session = last = next;
        return Map.of("file", file.toString(), "width", next.width, "height", next.height);
    }

    static Map<String, Object> status() {
        Session running = session;
        if (running != null && System.nanoTime() >= running.end) {
            session = null;
            running.finish();
        }
        Session s = last;
        if (s == null) return Map.of("state", "idle");
        var result = new java.util.LinkedHashMap<String, Object>();
        result.put("state", s.done ? "done" : "recording");
        result.put("file", s.file.toString());
        result.put("frames", s.written);
        result.put("dropped", s.dropped);
        // Bot clients treat any "error" key as failure, so a successful recording omits it.
        if (s.error != null) result.put("error", String.valueOf(s.error));
        return result;
    }

    /** Render thread, after the frame is complete and before it is shown. */
    public static void capture() {
        Session s = session;
        if (s == null) return;
        long now = System.nanoTime();
        RenderTarget target = Minecraft.getInstance().getMainRenderTarget();
        if (now >= s.end || target.width != s.width || target.height != s.height) {
            session = null;
            s.finish();
            return;
        }
        ByteBuffer buffer = s.free.poll();
        if (buffer == null) {
            // The writer is behind; a missing frame is counted rather than stalling the game.
            s.dropped++;
            return;
        }
        GlStateManager._glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, target.frameBufferId);
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1);
        buffer.clear();
        GL11.glReadPixels(0, 0, s.width, s.height, GL11.GL_RGB, GL11.GL_UNSIGNED_BYTE, buffer);
        GlStateManager._glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, 0);
        s.frames.add(new Frame(now, buffer));
    }

    private record Frame(long nanos, ByteBuffer pixels) {}

    private static final class Session {
        final Path file;
        final int width;
        final int height;
        final long end;
        final BlockingQueue<ByteBuffer> free = new ArrayBlockingQueue<>(POOL);
        final BlockingQueue<Frame> frames = new ArrayBlockingQueue<>(POOL + 1);
        volatile int written;
        volatile int dropped;
        volatile boolean done;
        volatile Exception error;

        Session(Path file, int width, int height, double seconds) throws IOException {
            this.file = file;
            this.width = width;
            this.height = height;
            this.end = System.nanoTime() + (long) (seconds * 1e9);
            for (int i = 0; i < POOL; i++) free.add(MemoryUtil.memAlloc(width * height * 3));
            DataOutputStream out =
                    new DataOutputStream(
                            new BufferedOutputStream(Files.newOutputStream(file), 1 << 20));
            out.writeBytes("BFREC1");
            out.writeInt(width);
            out.writeInt(height);
            Thread writer = new Thread(() -> write(out), "bf-testbot-recorder");
            writer.setDaemon(true);
            writer.start();
        }

        void finish() {
            frames.add(new Frame(-1, null));
        }

        private void write(DataOutputStream out) {
            Deflater deflater = new Deflater(Deflater.BEST_SPEED);
            byte[] raw = new byte[width * height * 3];
            byte[] packed = new byte[raw.length + raw.length / 100 + 64];
            try (out) {
                while (true) {
                    Frame frame = frames.take();
                    if (frame.pixels == null) break;
                    frame.pixels.get(0, raw);
                    free.add(frame.pixels);
                    deflater.reset();
                    deflater.setInput(raw);
                    deflater.finish();
                    int length = deflater.deflate(packed);
                    out.writeLong(frame.nanos);
                    out.writeInt(length);
                    out.write(packed, 0, length);
                    written++;
                }
            } catch (IOException | InterruptedException | RuntimeException e) {
                error = e;
            } finally {
                deflater.end();
                ByteBuffer buffer;
                while ((buffer = free.poll()) != null) MemoryUtil.memFree(buffer);
                done = true;
            }
        }
    }
}
