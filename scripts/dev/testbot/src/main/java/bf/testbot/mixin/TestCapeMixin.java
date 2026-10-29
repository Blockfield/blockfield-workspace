package bf.testbot.mixin;

import net.minecraft.client.player.AbstractClientPlayer;
import net.minecraft.client.resources.PlayerSkin;
import net.minecraft.resources.ResourceLocation;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * An opt-in local cape fixture: offline QA identities do not have Mojang capes. Never ship testbot.
 */
@Mixin(AbstractClientPlayer.class)
public abstract class TestCapeMixin {
    @Inject(method = "getSkin", at = @At("RETURN"), cancellable = true)
    private void testbot$cape(CallbackInfoReturnable<PlayerSkin> cir) {
        if (!Boolean.getBoolean("bf.testbot.cape")) return;
        var skin = cir.getReturnValue();
        cir.setReturnValue(
                new PlayerSkin(
                        skin.texture(),
                        skin.textureUrl(),
                        ResourceLocation.withDefaultNamespace("textures/entity/elytra.png"),
                        skin.elytraTexture(),
                        skin.model(),
                        skin.secure()));
    }
}
