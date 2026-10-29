package bf.testbot.mixin;

import net.minecraft.client.MouseHandler;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(MouseHandler.class)
public interface MouseHandlerInvoker {
    @Invoker("onPress")
    void bf$onPress(long window, int button, int action, int modifiers);

    @Invoker("onMove")
    void bf$onMove(long window, double x, double y);

    @Invoker("onScroll")
    void bf$onScroll(long window, double xOffset, double yOffset);
}
