package bf.testbot.mixin;

import net.minecraft.client.Minecraft;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(Minecraft.class)
public interface MinecraftInputAccess {
    @Invoker("startUseItem")
    void bf$startUseItem();

    @Accessor("rightClickDelay")
    int bf$rightClickDelay();
}
