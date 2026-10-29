package bf.testbot.mixin;

import bf.testbot.RenderedPlayerGeometry;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;
import com.mojang.blaze3d.vertex.PoseStack;
import com.mojang.blaze3d.vertex.VertexConsumer;

import net.minecraft.client.model.EntityModel;
import net.minecraft.client.renderer.entity.LivingEntityRenderer;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;

@Mixin(LivingEntityRenderer.class)
public class PlayerGeometryRendererMixin {
    @WrapOperation(
            method =
                    "render(Lnet/minecraft/world/entity/LivingEntity;FFLcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/MultiBufferSource;I)V",
            at =
                    @At(
                            value = "INVOKE",
                            target =
                                    "Lnet/minecraft/client/model/EntityModel;renderToBuffer(Lcom/mojang/blaze3d/vertex/PoseStack;Lcom/mojang/blaze3d/vertex/VertexConsumer;III)V"))
    private void bf$geometryBody(
            EntityModel<?> model,
            PoseStack poses,
            VertexConsumer vertices,
            int light,
            int overlay,
            int color,
            Operation<Void> original) {
        boolean capturing = RenderedPlayerGeometry.beginModel(model);
        boolean success = false;
        try {
            original.call(model, poses, vertices, light, overlay, color);
            success = true;
        } finally {
            if (capturing) RenderedPlayerGeometry.endModel(success);
        }
    }
}
