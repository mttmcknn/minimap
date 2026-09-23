package dev.minimap;

import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.Build;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;

/** One fresh accessibility observation. No inputs, saved UI data, or background service. */
public final class MinimapLayout {
    private static int visited;

    private static void text(JSONObject item, String key, CharSequence value) throws Exception {
        if (value != null && !value.toString().trim().isEmpty()) {
            if (value.length() > 32768) throw new IllegalStateException("UI text exceeds limit");
            item.put(key, value.toString());
        }
    }

    private static void flag(JSONArray flags, String name, boolean value) {
        if (value) flags.put(name);
    }

    private static void walk(AccessibilityNodeInfo node, Rect window, JSONArray result, int depth)
            throws Exception {
        if (++visited > 10000 || depth > 100) throw new IllegalStateException("UI tree exceeds limit");
        if (!node.isVisibleToUser()) return;
        JSONObject item = new JSONObject();
        text(item, "text", node.getText());
        text(item, "content-desc", node.getContentDescription());
        JSONArray interactions = new JSONArray();
        flag(interactions, "checkable", node.isCheckable());
        flag(interactions, "clickable", node.isClickable());
        flag(interactions, "focusable", node.isFocusable());
        flag(interactions, "scrollable", node.isScrollable());
        flag(interactions, "long-clickable", node.isLongClickable());
        flag(interactions, "password", node.isPassword());
        if (interactions.length() > 0) item.put("interactions", interactions);
        JSONArray state = new JSONArray();
        flag(state, "checked", node.isChecked());
        flag(state, "focused", node.isFocused());
        flag(state, "selected", node.isSelected());
        if (state.length() > 0) item.put("state", state);

        // Match Android CLI's semantic-node selection and shortened resource IDs
        // so existing graphs keep the same fingerprint. Add actionability and
        // privacy metadata without treating container classes as screen identity.
        if (item.length() > 0) {
            String id = node.getViewIdResourceName();
            if (id != null) {
                int prefix = id.indexOf(":id/");
                text(item, "resource-id", prefix < 0 ? id : id.substring(prefix + 4));
            }
            Rect bounds = new Rect();
            node.getBoundsInScreen(bounds);
            if (bounds.isEmpty() || !bounds.intersect(window)) {
                item.put("off-screen", true);
            } else {
                item.put("center", "[" + bounds.centerX() + "," + bounds.centerY() + "]");
                if (node.isScrollable()) {
                    item.put("bounds", "[" + bounds.left + "," + bounds.top + "]["
                            + bounds.right + "," + bounds.bottom + "]");
                }
            }
            item.put("enabled", node.isEnabled());
            if (node.isPassword()) item.put("password", true);
            if (node.isEditable()) item.put("editable", true);
            result.put(item);
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            AccessibilityNodeInfo child = node.getChild(index);
            if (child != null) {
                try { walk(child, window, result, depth + 1); }
                finally { child.recycle(); }
            }
        }
    }

    public static void main(String[] args) {
        int status = 1;
        String stage = "connection";
        try {
            // The stock uiautomator command uses this shell bridge too. It is
            // not an SDK API: the host falls back to Android CLI if unavailable.
            Class<?> type = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            Object wrapper = type.getConstructor().newInstance();
            type.getMethod("connect").invoke(wrapper);
            try {
                type.getMethod("setCompressedLayoutHierarchy", boolean.class).invoke(wrapper, false);
                UiAutomation ui = (UiAutomation) type.getMethod("getUiAutomation").invoke(wrapper);
                stage = "idle wait";
                ui.waitForIdle(100, 3000);
                // A new connection for every invocation prevents cross-capture
                // accessibility caches from supplying the previous screen.
                // During a window transition the root can briefly be absent.
                // Retry within this connection rather than restarting capture.
                long deadline = SystemClock.uptimeMillis() + 1500;
                for (int attempt = 0; attempt < 16; attempt++) {
                    stage = "root observation";
                    if (Build.VERSION.SDK_INT >= 33) ui.clearCache();
                    AccessibilityNodeInfo root = ui.getRootInActiveWindow();
                    if (root != null) {
                        try {
                            Rect window = new Rect();
                            root.getBoundsInScreen(window);
                            JSONArray result = new JSONArray();
                            visited = 0;
                            stage = "tree traversal";
                            walk(root, window, result, 0);
                            String json = result.toString();
                            if (json.length() > 2_000_000) {
                                throw new IllegalStateException("UI observation outside size limits");
                            }
                            if (result.length() > 0) {
                                System.out.println(json);
                                status = 0;
                                break;
                            }
                        } finally { root.recycle(); }
                    }
                    if (SystemClock.uptimeMillis() >= deadline) break;
                    SystemClock.sleep(100);
                }
                if (status != 0) throw new IllegalStateException("UI root unavailable");
            } finally { type.getMethod("disconnect").invoke(wrapper); }
        } catch (Exception error) {
            status = 1;
            System.err.println("Minimap could not capture a fresh UI tree during " + stage + " ("
                    + error.getClass().getSimpleName() + ")");
        }
        System.exit(status);
    }
}
