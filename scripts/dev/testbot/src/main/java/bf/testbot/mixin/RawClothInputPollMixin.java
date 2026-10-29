package bf.testbot.mixin;

import bf.testbot.TestBot;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;

@Pseudo
@Mixin(targets = "me.shedaniel.clothconfig2.api.ModifierKeyCode", remap = false)
public interface RawClothInputPollMixin {
    @WrapOperation(
            method = "matchesCurrentMouse",
            at = @At(value = "INVOKE", target = "Lorg/lwjgl/glfw/GLFW;glfwGetMouseButton(JI)I"))
    private int bf$mouse(long window, int button, Operation<Integer> original) {
        Boolean down = TestBot.rawKeyPressed(window, true, button);
        return down == null ? original.call(window, button) : (down ? 1 : 0);
    }
}
