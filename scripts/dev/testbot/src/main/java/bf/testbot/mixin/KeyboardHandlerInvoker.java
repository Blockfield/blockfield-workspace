package bf.testbot.mixin;

import net.minecraft.client.KeyboardHandler;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(KeyboardHandler.class)
public interface KeyboardHandlerInvoker {
    @Invoker("charTyped")
    void bf$charTyped(long window, int character, int modifiers);
}
