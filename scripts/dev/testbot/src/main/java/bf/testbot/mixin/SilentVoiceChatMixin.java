package bf.testbot.mixin;

import bf.testbot.HeadlessBackend;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Pseudo;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Pseudo
@Mixin(targets = "de.maxhenkel.voicechat.voice.client.ClientManager", remap = false)
public class SilentVoiceChatMixin {
    @Inject(
            method = {"onJoinWorld", "authenticate"},
            at = @At("HEAD"),
            cancellable = true)
    private void silentVoiceChat(CallbackInfo ci) {
        // PTT/muting still opens the microphone; headless tests use file audio only.
        if ("null".equals(System.getenv("ALSOFT_DRIVERS"))) {
            HeadlessBackend.voiceChatBlocked = true;
            ci.cancel();
        }
    }
}
