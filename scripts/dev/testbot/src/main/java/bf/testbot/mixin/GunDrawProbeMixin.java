package bf.testbot.mixin;

import bf.testbot.TestBot;

import net.minecraft.world.item.ItemStack;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Pseudo
@Mixin(targets = "com.tacz.guns.client.renderer.item.AnimateGeoItemRenderer$1", remap = false)
public class GunDrawProbeMixin {
    @Shadow private boolean drawn;
    @Shadow private ItemStack lastItem;

    @Inject(method = "triggerDraw", at = @At("HEAD"), remap = false)
    private void probeDraw(CallbackInfo ci) {
        if (!drawn) TestBot.recordGunEvent("draw", lastItem);
    }

    @Inject(method = "triggerPutAway", at = @At("HEAD"), remap = false)
    private void probePutAway(CallbackInfo ci) {
        TestBot.recordGunEvent("putAway", lastItem);
    }
}
