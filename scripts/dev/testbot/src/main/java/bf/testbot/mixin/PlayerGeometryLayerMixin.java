package bf.testbot.mixin;

import bf.testbot.RenderedPlayerGeometry;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;
import com.mojang.blaze3d.vertex.PoseStack;

import net.minecraft.client.model.HumanoidModel;
import net.minecraft.client.renderer.MultiBufferSource;
import net.minecraft.client.renderer.entity.LivingEntityRenderer;
import net.minecraft.client.renderer.entity.layers.RenderLayer;
import net.minecraft.world.entity.Entity;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Attributes layer geometry to the player-geometry oracle: every RenderLayer render inside
 * LivingEntityRenderer.render opens a layer scope, and the layer model's bones are registered when
 * they inherit the tracked model's pose. One file holds both interception points because a mixin
 * class has a single target.
 */
@Mixin(LivingEntityRenderer.class)
public class PlayerGeometryLayerMixin {
    @WrapOperation(
            method =
                    "render(Lnet/minecraft/world/entity/LivingEntity;FFLcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/MultiBufferSource;I)V",
            at =
                    @At(
                            value = "INVOKE",
                            target =
                                    "Lnet/minecraft/client/renderer/entity/layers/RenderLayer;render(Lcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/MultiBufferSource;ILnet/minecraft/world/entity/Entity;FFFFFF)V"))
    private void bf$geometryLayer(
            RenderLayer<?, ?> layer,
            PoseStack poses,
            MultiBufferSource buffers,
            int light,
            Entity entity,
            float limbSwing,
            float limbSwingAmount,
            float partialTick,
            float age,
            float headYaw,
            float headPitch,
            Operation<Void> original) {
        boolean capturing = RenderedPlayerGeometry.beginLayer(entity, layer);
        try {
            original.call(
                    layer,
                    poses,
                    buffers,
                    light,
                    entity,
                    limbSwing,
                    limbSwingAmount,
                    partialTick,
                    age,
                    headYaw,
                    headPitch);
        } finally {
            if (capturing) RenderedPlayerGeometry.endLayer(entity, layer);
        }
    }

    @Mixin(HumanoidModel.class)
    public static class Copy {
        @Inject(
                method = "copyPropertiesTo(Lnet/minecraft/client/model/HumanoidModel;)V",
                at = @At("RETURN"))
        private void bf$geometryCopy(HumanoidModel<?> model, CallbackInfo ci) {
            RenderedPlayerGeometry.copyLayerModel((HumanoidModel<?>) (Object) this, model);
        }
    }
}
