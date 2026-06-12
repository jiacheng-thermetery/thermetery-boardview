# Minification is OFF for v1 (isMinifyEnabled = false). These rules are kept
# so flipping it on later does not silently break the WebView bridge.

# The JS bridge methods are called reflectively by the WebView.
-keepclassmembers class com.thermetery.boardview.BoardviewBridge {
    @android.webkit.JavascriptInterface <methods>;
}
