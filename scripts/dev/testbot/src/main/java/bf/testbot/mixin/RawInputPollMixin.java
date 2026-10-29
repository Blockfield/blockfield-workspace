package bf.testbot.mixin;

import bf.testbot.TestBot;

import com.mojang.blaze3d.platform.InputConstants;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(InputConstants.class)
public class RawInputPollMixin {
    @Inject(method = "isKeyDown", at = @At("HEAD"), cancellable = true)
    private static void bf$key(long window, int key, CallbackInfoReturnable<Boolean> ci) {
        Boolean down = TestBot.rawKeyPressed(window, false, key);
        if (down != null) ci.setReturnValue(down);
    }
}
