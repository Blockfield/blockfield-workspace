package bf.testbot.mixin;

import bf.testbot.TestBot;

import com.mojang.blaze3d.vertex.PoseStack;

import net.minecraft.client.player.LocalPlayer;
import net.minecraft.client.renderer.MultiBufferSource;
import net.minecraft.world.item.ItemDisplayContext;
import net.minecraft.world.item.ItemStack;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Pseudo
@Mixin(targets = "com.tacz.guns.client.renderer.item.GunItemRendererWrapper", remap = false)
public class GunRenderProbeMixin {
    @Inject(method = "tryExit", at = @At("HEAD"), remap = false)
    private void probeExit(ItemStack stack, long duration, CallbackInfo ci) {
        TestBot.recordGunEvent("exit", stack);
    }

    @Inject(method = "renderFirstPerson", at = @At("HEAD"), remap = false)
    private void probeRender(
            LocalPlayer player,
            ItemStack stack,
            ItemDisplayContext context,
            PoseStack pose,
            MultiBufferSource buffers,
            int light,
            float partial,
            CallbackInfo ci) {
        TestBot.recordGunEvent("render", stack, context);
    }
}
