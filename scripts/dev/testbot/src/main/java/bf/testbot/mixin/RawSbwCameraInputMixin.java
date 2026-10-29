package bf.testbot.mixin;

import bf.testbot.TestBot;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;

@Pseudo
@Mixin(targets = "com.atsuishio.superbwarfare.event.ClientEventHandler", remap = false)
public class RawSbwCameraInputMixin {
    @WrapOperation(
            method = "handlePlayerCamera",
            at = @At(value = "INVOKE", target = "Lorg/lwjgl/glfw/GLFW;glfwGetKey(JI)I"))
    private int bf$key(long window, int key, Operation<Integer> original) {
        Boolean down = TestBot.rawKeyPressed(window, false, key);
        return down == null ? original.call(window, key) : (down ? 1 : 0);
    }
}
