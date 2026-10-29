package bf.testbot.mixin;

import bf.testbot.TestBot;

import net.minecraft.client.Minecraft;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(Minecraft.class)
public class RawInputFocusMixin {
    @Inject(method = "isWindowActive", at = @At("HEAD"), cancellable = true)
    private void bf$focus(CallbackInfoReturnable<Boolean> ci) {
        if (TestBot.rawInputActive()) ci.setReturnValue(true);
    }
}
