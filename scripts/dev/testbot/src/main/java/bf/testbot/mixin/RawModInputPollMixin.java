package bf.testbot.mixin;

import bf.testbot.TestBot;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;

@Pseudo
@Mixin(
        targets = {
            "com.lunares.blockfield.client.gui.ClientEventHandler",
            "com.simibubi.create.AllKeys",
            "com.atsuishio.superbwarfare.client.MouseMovementHandler"
        },
        remap = false)
public class RawModInputPollMixin {
    // Verified static callers use either button or cursor polling, so the other API is optional.
    @WrapOperation(
            method = "*",
            at = @At(value = "INVOKE", target = "Lorg/lwjgl/glfw/GLFW;glfwGetMouseButton(JI)I"),
            require = 0)
    private static int bf$mouse(long window, int button, Operation<Integer> original) {
        Boolean down = TestBot.rawKeyPressed(window, true, button);
        return down == null ? original.call(window, button) : (down ? 1 : 0);
    }

    @WrapOperation(
            method = "*",
            at = @At(value = "INVOKE", target = "Lorg/lwjgl/glfw/GLFW;glfwGetCursorPos(J[D[D)V"),
            require = 0)
    private static void bf$cursor(long window, double[] x, double[] y, Operation<Void> original) {
        int[] point = TestBot.rawCursor(window);
        if (point == null) original.call(window, x, y);
        else {
            x[0] = point[0];
            y[0] = point[1];
        }
    }
}
