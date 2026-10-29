package bf.testbot.mixin;

import bf.testbot.RenderedPlayerGeometry;

import com.mojang.blaze3d.vertex.PoseStack;

import net.minecraft.client.Camera;
import net.minecraft.client.renderer.MultiBufferSource;
import net.minecraft.client.renderer.entity.EntityRenderDispatcher;
import net.minecraft.world.entity.Entity;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(EntityRenderDispatcher.class)
public class PlayerGeometryDispatcherMixin {
    @Shadow public Camera camera;

    @Inject(
            method =
                    "render(Lnet/minecraft/world/entity/Entity;DDDFFLcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/MultiBufferSource;I)V",
            at = @At("HEAD"))
    private void bf$geometryBegin(
            Entity entity,
            double x,
            double y,
            double z,
            float yaw,
            float partial,
            PoseStack poses,
            MultiBufferSource buffers,
            int light,
            CallbackInfo ci) {
        RenderedPlayerGeometry.begin(entity, x, y, z, partial, poses, camera);
    }

    @Inject(
            method =
                    "render(Lnet/minecraft/world/entity/Entity;DDDFFLcom/mojang/blaze3d/vertex/PoseStack;Lnet/minecraft/client/renderer/MultiBufferSource;I)V",
            at = @At("RETURN"))
    private void bf$geometryEnd(
            Entity entity,
            double x,
            double y,
            double z,
            float yaw,
            float partial,
            PoseStack poses,
            MultiBufferSource buffers,
            int light,
            CallbackInfo ci) {
        RenderedPlayerGeometry.end(entity);
    }
}
