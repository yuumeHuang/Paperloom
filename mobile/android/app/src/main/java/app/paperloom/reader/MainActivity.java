package app.paperloom.reader;

import com.getcapacitor.BridgeActivity;
import android.os.Bundle;
import androidx.activity.OnBackPressedCallback;

public class MainActivity extends BridgeActivity {
    @Override protected void onCreate(Bundle state) {
        registerPlugin(PaperloomNativePlugin.class);
        super.onCreate(state);
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() {
                if (getBridge() != null) getBridge().triggerJSEvent("paperloomBackButton", "document");
                else moveTaskToBack(true);
            }
        });
    }
}
