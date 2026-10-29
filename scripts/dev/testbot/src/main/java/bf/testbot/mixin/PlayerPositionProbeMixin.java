package bf.testbot.mixin;

import bf.testbot.AcceptanceProbes;

import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientPacketListener;
import net.minecraft.network.protocol.game.ClientboundPlayerPositionPacket;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ClientPacketListener.class)
public class PlayerPositionProbeMixin {
    // RETURN runs only after the packet's main-thread handoff and relative-position application.
    @Inject(method = "handleMovePlayer", at = @At("RETURN"))
    private void bf$positionApplied(ClientboundPlayerPositionPacket packet, CallbackInfo ci) {
        AcceptanceProbes.positionApplied(Minecraft.getInstance());
    }
}
