package bf.testbot.mixin;

import bf.testbot.RenderedPlayerGeometry;

import com.mojang.blaze3d.vertex.VertexConsumer;

import net.minecraft.client.model.geom.ModelPart;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.ModifyVariable;

@Mixin(ModelPart.class)
public class PlayerGeometryPartMixin {
    @ModifyVariable(
            method =
                    "compile(Lcom/mojang/blaze3d/vertex/PoseStack$Pose;Lcom/mojang/blaze3d/vertex/VertexConsumer;III)V",
            at = @At("HEAD"),
            argsOnly = true)
    private VertexConsumer bf$geometryVertices(VertexConsumer vertices) {
        return RenderedPlayerGeometry.observe((ModelPart) (Object) this, vertices);
    }
}
