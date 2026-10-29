package bf.testbot.mixin;

import bf.testbot.FrameRecorder;

import net.minecraft.client.Minecraft;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(Minecraft.class)
public class RecordFrameMixin {
    @Inject(
            method = "runTick",
            at =
                    @At(
                            value = "INVOKE",
                            target = "Lcom/mojang/blaze3d/pipeline/RenderTarget;blitToScreen(II)V"))
    private void recordFrame(boolean tick, CallbackInfo ci) {
        FrameRecorder.capture();
    }
}
