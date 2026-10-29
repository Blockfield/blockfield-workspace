package bf.testbot.mixin;

import bf.testbot.TestBot;

import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Pseudo
@Mixin(targets = "com.tacz.guns.client.renderer.item.AnimateGeoItemRenderer", remap = false)
public class GunInitProbeMixin {
    @Inject(method = "tryInit", at = @At("HEAD"), remap = false)
    private void probeInit(ItemStack stack, Player player, float partial, CallbackInfo ci) {
        TestBot.recordGunEvent("init", stack);
    }
}
