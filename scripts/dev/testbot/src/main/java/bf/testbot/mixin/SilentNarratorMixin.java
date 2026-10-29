package bf.testbot.mixin;

import com.llamalad7.mixinextras.injector.wrapoperation.Operation;
import com.llamalad7.mixinextras.injector.wrapoperation.WrapOperation;
import com.mojang.text2speech.Narrator;

import net.minecraft.client.GameNarrator;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;

// ModernFix's Linux wrapper initializes speech directly; this wrapper must run outside it.
@Mixin(value = GameNarrator.class, priority = 1100)
public class SilentNarratorMixin {
    @WrapOperation(
            method = "<init>",
            at =
                    @At(
                            value = "INVOKE",
                            remap = false,
                            target =
                                    "Lcom/mojang/text2speech/Narrator;getNarrator()Lcom/mojang/text2speech/Narrator;"))
    private Narrator silentNarrator(Operation<Narrator> original) {
        // OpenAL's null driver does not silence native speech or prevent its initialization.
        return "null".equals(System.getenv("ALSOFT_DRIVERS")) ? Narrator.EMPTY : original.call();
    }
}
