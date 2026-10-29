package bf.testbot.mixin;

import bf.testbot.TestBot;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Coerce;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Pseudo
@Mixin(targets = "com.lunares.blockfield.client.workshop.WorkshopAiEffects", remap = false)
public class WorkshopAiPreviewProbeMixin {
    @Inject(
            method =
                    "acceptPreview(Lcom/lunares/blockfield/network/packet/WorkshopAiPreviewS2CPacket;)V",
            at = @At("TAIL"),
            remap = false)
    private static void captureApprovedPreview(@Coerce Object packet, CallbackInfo ci) {
        TestBot.captureWorkshopAiPreview(packet);
    }
}
