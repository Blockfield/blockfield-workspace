package bf.testbot.mixin;

import bf.testbot.TestBot;

import net.minecraft.client.MouseHandler;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(MouseHandler.class)
public class RawInputMouseGrabMixin {
    @Inject(method = "isMouseGrabbed", at = @At("HEAD"), cancellable = true)
    private void bf$grabbed(CallbackInfoReturnable<Boolean> ci) {
        if (TestBot.rawInputActive()) ci.setReturnValue(true);
    }
}
