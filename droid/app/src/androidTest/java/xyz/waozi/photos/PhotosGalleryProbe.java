package xyz.waozi.photos;

import android.app.Instrumentation;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.util.Arrays;
import java.nio.charset.StandardCharsets;

/** Exercises the real activity/provider/JNI on a test-owned emulator only. */
public final class PhotosGalleryProbe extends Instrumentation {
    private String stage;
    private PhotosActivity activity;
    private final JSONObject checks = new JSONObject();

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        stage = arguments == null ? "full" : arguments.getString("stage", "full");
        start();
    }

    private void require(boolean passed, String name) throws Exception {
        if (!passed) throw new AssertionError(name);
        checks.put(name, true);
        android.util.Log.i("PhotosProbe", stage + ": " + name);
    }

    private JSONObject state() throws Exception {
        return new JSONObject(new String(activity.galleryState(), StandardCharsets.UTF_8));
    }

    private JSONObject settled() throws Exception {
        long deadline = SystemClock.uptimeMillis() + 30000;
        while (SystemClock.uptimeMillis() < deadline) {
            JSONObject value = state();
            if (value.optInt("loading") == 0) return value;
            SystemClock.sleep(100);
        }
        throw new AssertionError("gallery did not settle");
    }

    private JSONObject galleryTotal(int expected) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            JSONObject value = state();
            if (value.optInt("loading") == 0 && value.optInt("total") == expected) return value;
            SystemClock.sleep(100);
        }
        throw new AssertionError("resumed gallery did not reach its expected provider total");
    }

    private JSONObject viewerAt(String expected) throws Exception {
        // Provider publication precedes the native frame's polling and view
        // transition. Send the next gesture only after Photos observes it.
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            JSONObject value = state();
            if (value.optInt("loading") == 0 && value.optString("selected").equals(expected)
                    && value.getJSONArray("photos").length() == 1
                    && new String(activity.nativeViewerName, StandardCharsets.UTF_8)
                        .equals(value.getJSONArray("photos").getJSONObject(0).getString("name"))) {
                return value;
            }
            SystemClock.sleep(100);
        }
        throw new AssertionError("native viewer did not reach selected original " + expected);
    }

    private JSONObject action(String action, String id, int page) throws Exception {
        JSONObject request = new JSONObject().put("action", action).put("id", id).put("view", id).put("page", page);
        activity.galleryAction(request.toString());
        // Security actions are posted to the activity thread. Observe their
        // completion before accepting an older, already-settled gallery state.
        runOnMainSync(() -> {});
        SystemClock.sleep(250);
        return settled();
    }

    private void screenshot(String name) throws Exception {
        SystemClock.sleep(500);
        Bitmap bitmap = getUiAutomation().takeScreenshot();
        if (bitmap == null) throw new AssertionError("screenshot unavailable");
        try (FileOutputStream output = new FileOutputStream(new File(activity.getFilesDir(), name + ".png"))) {
            if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) throw new AssertionError("screenshot failed");
        } finally { bitmap.recycle(); }
    }

    private void tap(float x, float y) throws Exception {
        tap(x, y, 500);
    }

    private void tap(float x, float y, int wait) throws Exception {
        long down = SystemClock.uptimeMillis();
        MotionEvent press = MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, x, y, 0);
        press.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        require(getUiAutomation().injectInputEvent(press, true), "touch_press_delivered");
        press.recycle();
        SystemClock.sleep(120);
        MotionEvent release = MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0);
        release.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        require(getUiAutomation().injectInputEvent(release, true), "touch_release_delivered");
        release.recycle();
        SystemClock.sleep(wait);
    }

    private void swipe(float from, float to, float y) throws Exception {
        long down = SystemClock.uptimeMillis();
        for (int i = 0; i <= 12; i++) {
            int action = i == 0 ? MotionEvent.ACTION_DOWN : i == 12 ? MotionEvent.ACTION_UP : MotionEvent.ACTION_MOVE;
            MotionEvent event = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, from + (to-from)*i/12, y, 0);
            event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            require(getUiAutomation().injectInputEvent(event, true), "swipe_events_delivered");
            event.recycle();
            SystemClock.sleep(25);
        }
        android.util.Log.i("PhotosProbe", "swipe injection elapsed_ms=" + (SystemClock.uptimeMillis() - down));
        SystemClock.sleep(500);
    }

    private int previewPixel() throws Exception {
        SystemClock.sleep(250);
        Bitmap picture = getUiAutomation().takeScreenshot();
        if (picture == null) throw new AssertionError("screenshot unavailable");
        try { return picture.getPixel(240, 280); }
        finally { picture.recycle(); }
    }

    private AccessibilityNodeInfo entry(AccessibilityNodeInfo node) {
        if (node == null) return null;
        if (node.isEditable() && node.isVisibleToUser() && node.isFocused()) return node;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo found = entry(node.getChild(i));
            if (found != null) return found;
        }
        return null;
    }

    private AccessibilityNodeInfo choice(AccessibilityNodeInfo node, String pattern, boolean photo) {
        if (node == null) return null;
        String text = node.getText() == null ? "" : node.getText().toString();
        String description = node.getContentDescription() == null ? "" : node.getContentDescription().toString();
        if (node.isVisibleToUser() && (photo ? description.matches(pattern) : text.matches(pattern))) return node;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo result = choice(node.getChild(i), pattern, photo);
            if (result != null) return result;
        }
        return null;
    }

    private void choose(String pattern, boolean photo) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            AccessibilityNodeInfo node = choice(getUiAutomation().getRootInActiveWindow(), pattern, photo);
            if (node != null) {
                // Android publishes accessibility nodes before the dialog's
                // transition and touch listener are ready. Reacquire after
                // the actual system UI settles, as for device credentials.
                getUiAutomation().waitForIdle(500, 5000);
                node = choice(getUiAutomation().getRootInActiveWindow(), pattern, photo);
                if (node == null || !node.isEnabled()) continue;
                android.graphics.Rect bounds = new android.graphics.Rect(); node.getBoundsInScreen(bounds);
                tap(bounds.centerX(), bounds.centerY()); return;
            }
            SystemClock.sleep(100);
        }
        throw new AssertionError("system photo selection control missing: " + pattern);
    }

    private void selectedChecks() throws Exception {
        action("access", "", 0);
        choose("(Select photos( and videos)?|Allow limited access)", false);
        choose("Photo taken.*", true);
        choose("(Add|Done)(.*)|Allow \\(1\\)", false);
        JSONObject value = galleryTotal(1);
        require(value.getInt("access") == 4 && value.getJSONArray("photos").length() == 1,
            "android_selected_only_grant_exposes_exactly_one_chosen_original");
        BackupQueue queue = new BackupQueue(activity, () -> {}, false);
        try {
            require(queue.scan(() -> false) && queue.queued == 1 && queue.paused > 0,
                "camera_queue_resumes_only_os_selected_original_and_pauses_ungranted_items");
        } finally { queue.close(); }
        screenshot("selected");
    }

    private JSONObject authenticate() throws Exception {
        // The credential field can appear in accessibility before SystemUI
        // installs its submit listener. Wait for that real OS transition.
        getUiAutomation().waitForIdle(500, 5000);
        boolean entered = false;
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            AccessibilityNodeInfo input = entry(getUiAutomation().getRootInActiveWindow());
            if (input != null) {
                Bundle text = new Bundle(); text.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "2468");
                require(input.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, text), "test_device_pin_input");
                SystemClock.sleep(300);
                shell("input keyevent 66");
                entered = true;
                break;
            }
            SystemClock.sleep(100);
        }
        require(entered, "system_credential_field_ready");
        deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            JSONObject value = state();
            if (value.getInt("auth_pending") == 0) { SystemClock.sleep(500); return settled(); }
            SystemClock.sleep(100);
        }
        throw new AssertionError("device authentication did not finish");
    }

    private void cancelAuthentication() throws Exception {
        // The first Back can dismiss the credential keyboard rather than the
        // system prompt. Stop as soon as Photos receives the canceled result.
        for (int i = 0; i < 4 && state().getInt("auth_pending") != 0; i++) {
            getUiAutomation().performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK);
            SystemClock.sleep(500);
        }
        require(state().getInt("auth_pending") == 0, "authentication_cancel_result_received");
        getUiAutomation().waitForIdle(500, 5000);
    }

    private void lockChecks(JSONObject value) throws Exception {
        final int originals = value.getInt("total");
        require(originals > 0, "authentication_has_synthetic_media");
        require(value.getInt("lock_enabled") == 0, "device_lock_initially_off");
        value = action("enable_lock", "", 0);
        require(value.getInt("auth_pending") == 1, "device_lock_requires_authentication");
        // Canceling enrollment does not create a lock or retain a grant.
        SystemClock.sleep(750);
        cancelAuthentication();
        require(state().getInt("lock_enabled") == 0 && state().getInt("auth_pending") == 0, "canceled_enrollment_keeps_lock_off");
        action("enable_lock", "", 0);
        value = authenticate();
        require(value.getInt("lock_enabled") == 1 && value.getInt("locked") == 0 && value.getInt("total") == originals, "authenticated_device_lock_enabled");
        String first = value.getJSONArray("photos").getJSONObject(0).getString("id");
        action("open", first, 0);
        value = action("lock", "", 0);
        require(value.getInt("locked") == 1 && value.getJSONArray("photos").length() == 0 && value.getLong("selected") == -1, "locked_gallery_has_no_media");
        action("open", first, 0); action("view", "photos", 0); action("share", "", 0);
        require(state().getInt("locked") == 1 && state().getJSONArray("photos").length() == 0, "locked_adapter_refuses_media_actions");
        screenshot("locked");
        action("unlock", "", 0);
        SystemClock.sleep(750);
        cancelAuthentication();
        require(state().getInt("locked") == 1, "canceled_unlock_remains_locked");
        action("unlock", "", 0);
        value = authenticate();
        require(value.getInt("locked") == 0, "authenticated_device_unlock");
        action("back", "", 0);
        // Pause and resume the actual activity, preserving the private preference.
        getUiAutomation().performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_HOME);
        SystemClock.sleep(500);
        require(state().getJSONArray("photos").length() == 0 && state().getInt("locked") == 1, "background_relocks_and_clears_media");
        getTargetContext().startActivity(new Intent(Intent.ACTION_MAIN).setClassName(getTargetContext(), "xyz.waozi.photos.PhotosActivity")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        SystemClock.sleep(500);
        require(state().getInt("locked") == 1, "return_requires_authentication");
        action("unlock", "", 0); authenticate();
        value = action("hide_content", "", 0);
        require(value.getInt("hide_content") == 1, "content_privacy_enabled");
        final boolean[] secure = {false};
        runOnMainSync(() -> secure[0] = (activity.getWindow().getAttributes().flags & android.view.WindowManager.LayoutParams.FLAG_SECURE) != 0);
        require(secure[0], "os_screenshot_and_recent_app_protection");
        value = action("hide_content", "", 0);
        require(value.getInt("hide_content") == 0, "content_privacy_can_be_disabled");
        action("disable_lock", "", 0);
        value = authenticate();
        require(value.getInt("lock_enabled") == 0 && value.getInt("locked") == 0, "authenticated_device_lock_disabled");
    }

    private void backupChecks() throws Exception {
        byte[] recovery, expected;
        try (java.io.InputStream input = getContext().getAssets().open("recovery.bin")) { recovery = readBytes(input); }
        try (java.io.InputStream input = getContext().getAssets().open("expected.bin")) { expected = readBytes(input); }
        byte[] password = "Synthetic Android recovery password".getBytes(StandardCharsets.UTF_8);
        byte[] identity = activity.nativeReadBackupIdentity(recovery, password);
        require(identity != null && identity.length == 3968 && Arrays.equals(Arrays.copyOf(identity, 64), expected), "android_restores_desktop_account_and_node_pin");
        byte[] wrong = "Wrong synthetic Android password".getBytes(StandardCharsets.UTF_8);
        require(activity.nativeReadBackupIdentity(recovery, wrong) == null, "android_refuses_wrong_recovery_password");
        recovery[recovery.length-1] ^= 1;
        require(activity.nativeReadBackupIdentity(recovery, password) == null, "android_refuses_modified_recovery_file");
        recovery[recovery.length-1] ^= 1;
        File source = new File(activity.getFilesDir(), "synthetic-recovery");
        try (FileOutputStream output = new FileOutputStream(source)) { output.write(recovery); }
        java.lang.reflect.Field field = PhotosActivity.class.getDeclaredField("gallery"); field.setAccessible(true);
        DeviceGallery gallery = (DeviceGallery)field.get(activity);
        gallery.backupResult(android.app.Activity.RESULT_OK, new Intent().setData(android.net.Uri.fromFile(source)));
        activity.restoreBackup(password.clone());
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline && state().optInt("backup_pending") != 0) SystemClock.sleep(100);
        require(state().getInt("backup_configured") == 1 && state().getInt("backup_ready") == 1, "android_keystore_protects_recovered_backup_keys");
        byte[] saved;
        try (FileInputStream input = new FileInputStream(new File(activity.getFilesDir(), "backup-identity"))) { saved = readBytes(input); }
        require(saved.length == 3996 && !Arrays.equals(Arrays.copyOf(saved, 64), expected), "android_identity_is_sealed_on_disk");
        DeviceBackup reloaded = new DeviceBackup(activity, () -> true, () -> {});
        try {
            require(reloaded.configured && reloaded.ready, "android_recovered_identity_survives_reopen");
            byte[] read = reloaded.read();
            require(Arrays.equals(identity, read), "android_keystore_retains_original_content_and_signing_keys");
            Arrays.fill(read, (byte)0);
        } finally { reloaded.close(); }
        // A second restore must not replace the established account.
        activity.restoreBackup(wrong.clone());
        SystemClock.sleep(300);
        byte[] after;
        try (FileInputStream input = new FileInputStream(new File(activity.getFilesDir(), "backup-identity"))) { after = readBytes(input); }
        require(Arrays.equals(saved, after), "android_existing_account_is_not_overwritten");
        source.delete(); Arrays.fill(identity, (byte)0); Arrays.fill(recovery, (byte)0); Arrays.fill(password, (byte)0); Arrays.fill(wrong, (byte)0);
        queueChecks();
        screenshot("backup");
    }

    private JSONObject queued(int folders, int minimum) throws Exception {
        long deadline = SystemClock.uptimeMillis() + 15000;
        while (SystemClock.uptimeMillis() < deadline) {
            JSONObject value = settled();
            if (value.getInt("backup_folder_count") == folders && value.getInt("backup_queued") >= minimum
                && value.getInt("backup_scanning") == 0) return value;
            SystemClock.sleep(100);
        }
        throw new AssertionError("camera queue did not settle");
    }

    private File queueFile() { return new File(activity.getFilesDir(), "camera-backup/queue.sqlite"); }

    private void queueChecks() throws Exception {
        tap(180, 102);
        JSONObject groups = action("view", "albums", 0);
        JSONArray albums = groups.getJSONArray("albums");
        require(albums.length() >= 2, "camera_queue_has_distinct_device_folders");
        String image = null, video = null;
        for (int i = 0; i < albums.length(); i++) {
            JSONObject folder = albums.getJSONObject(i);
            if (folder.getString("name").equals("PhotosFixture")) image = folder.getString("id");
            if (folder.getString("name").equals("PhotosFixtureVideo")) video = folder.getString("id");
        }
        require(image != null && video != null, "camera_queue_folder_identity");
        JSONObject images = action("album", image, 0);
        int imageCount = images.getInt("total");
        require(imageCount > 0, "camera_queue_image_folder_has_originals");
        // Native UI button selects the folder; no provider file is opened.
        tap(240, 246);
        JSONObject value = queued(1, imageCount);
        require(value.getInt("backup_queued") == imageCount && value.getInt("backup_folder_selected") == 1,
            "native_folder_button_queues_only_selected_folder");
        BackupQueue reloaded = new BackupQueue(activity, () -> {}, false);
        try {
            require(reloaded.scan(() -> false) && reloaded.folderSelected(image) && reloaded.queued == imageCount,
                "camera_queue_and_choices_survive_adapter_reopen");
            android.app.job.JobScheduler scheduler = (android.app.job.JobScheduler)activity.getSystemService(android.content.Context.JOB_SCHEDULER_SERVICE);
            android.app.job.JobInfo job = scheduler.getPendingJob(BackupQueue.PERIODIC_JOB);
            require(job != null && job.isPersisted() && job.isPeriodic(), "camera_discovery_scheduled_across_process_and_device_restart");
            require(!reloaded.scan(() -> true) && reloaded.queued == 0 && reloaded.paused == imageCount,
                "interrupted_camera_scan_leaves_candidates_paused");
            require(reloaded.scan(() -> false) && reloaded.queued == imageCount,
                "camera_scan_resumes_without_duplicates");
            // Seed one synthetic remote stamp to check version retention. No
            // production path claims a backup before an upload acknowledgment.
            try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(queueFile().getPath(), null, 0)) {
                db.execSQL("UPDATE items SET confirmed=1 WHERE media_id=(SELECT MIN(media_id) FROM items)");
            }
            require(reloaded.scan(() -> false) && reloaded.confirmed == 1 && reloaded.queued == imageCount-1,
                "unchanged_camera_version_retains_confirmation");
            try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(queueFile().getPath(), null, 0)) {
                db.execSQL("UPDATE items SET generation=generation-1 WHERE confirmed=1");
            }
            require(reloaded.scan(() -> false) && reloaded.confirmed == 0 && reloaded.queued == imageCount,
                "changed_camera_version_rejoins_queue");
        } finally { reloaded.close(); }
        value = action("backup_folder", "", 0);
        value = queued(0, 0);
        require(value.getInt("backup_queued") == 0 && value.getInt("backup_paused") == imageCount,
            "deselected_camera_folder_pauses_its_queue");
        action("album", video, 0); action("backup_folder", "", 0);
        value = queued(1, 1);
        require(value.getInt("backup_queued") == 1, "camera_queue_handles_video_folder_separately");
        action("album", image, 0); action("backup_folder", "", 0);
        value = queued(2, imageCount+1);
        require(value.getInt("backup_queued") == imageCount+1, "camera_queue_includes_both_selected_folders");
        try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(queueFile().getPath(), null, 0);
             android.database.Cursor count = db.rawQuery("SELECT COUNT(*),SUM(confirmed) FROM items", null)) {
            require(count.moveToFirst() && count.getInt(0) == imageCount+1 && count.getInt(1) == 0,
                "camera_queue_preserves_unique_pending_versions_without_claiming_backup");
        }
        android.security.NetworkSecurityPolicy network = android.security.NetworkSecurityPolicy.getInstance();
        require(network.isCleartextTrafficPermitted("127.0.0.1") && !network.isCleartextTrafficPermitted("192.0.2.10")
            && !network.isCleartextTrafficPermitted("thinkpad.local") && !network.isCleartextTrafficPermitted("localhost"),
            "camera_backup_cleartext_reaches_only_this_phones_loopback");
        File stagedRoot = new File(activity.getFilesDir(), "camera-backup/staged");
        File outside = new File(activity.getFilesDir(), "linked-staging-fixture");
        require(outside.mkdirs(), "linked_staging_fixture_is_test_owned");
        File sentinel = new File(outside, "untouched");
        try (java.io.FileOutputStream marker = new java.io.FileOutputStream(sentinel)) { marker.write(42); }
        android.system.Os.symlink(outside.getAbsolutePath(), stagedRoot.getAbsolutePath());
        try {
            action("prepare_backup", "", 0);
            long refusalDeadline = SystemClock.uptimeMillis() + 10000;
            do {
                value = state();
                if (value.getInt("backup_preparing") == 0 && !value.getString("backup_prepare_failure").isEmpty()) break;
                SystemClock.sleep(100);
            } while (SystemClock.uptimeMillis() < refusalDeadline);
            require(value.getInt("backup_prepared") == 0 && value.getInt("backup_preparing") == 0,
                "linked_app_staging_path_is_refused_without_preparing_originals");
            require(outside.list().length == 1 && sentinel.length() == 1,
                "linked_staging_target_is_untouched");
        } finally { java.nio.file.Files.delete(stagedRoot.toPath()); }
        File cameraRoot = stagedRoot.getParentFile();
        File savedCameraRoot = new File(activity.getFilesDir(), "camera-backup-owned-fixture");
        require(!savedCameraRoot.exists() && cameraRoot.renameTo(savedCameraRoot),
            "camera_directory_link_fixture_preserves_the_original_queue");
        try {
            android.system.Os.symlink(outside.getAbsolutePath(), cameraRoot.getAbsolutePath());
            action("prepare_backup", "", 0);
            long refusalDeadline = SystemClock.uptimeMillis() + 10000;
            do {
                value = state();
                if (value.getInt("backup_preparing") == 0 && !value.getString("backup_prepare_failure").isEmpty()) break;
                SystemClock.sleep(100);
            } while (SystemClock.uptimeMillis() < refusalDeadline);
            require(value.getInt("backup_prepared") == 0 && value.getInt("backup_preparing") == 0
                && value.getString("backup_prepare_failure").equals("staging_directory"),
                "linked_camera_parent_is_refused_before_directory_creation");
            require(outside.list().length == 1 && sentinel.length() == 1
                && !new File(outside, "staged").exists() && !new File(outside, "queue.sqlite").exists(),
                "linked_camera_parent_target_receives_no_directories_or_queue");
        } finally {
            if (java.nio.file.Files.isSymbolicLink(cameraRoot.toPath())) java.nio.file.Files.delete(cameraRoot.toPath());
            require(savedCameraRoot.renameTo(cameraRoot), "camera_directory_link_fixture_restores_the_original_queue");
        }
        action("prepare_backup", "", 0);
        long deadline = SystemClock.uptimeMillis() + 45000;
        while (SystemClock.uptimeMillis() < deadline) {
            value = state();
            if (value.getInt("backup_prepared") == imageCount+1 && value.getInt("backup_preparing") == 0) break;
            if (value.getInt("backup_preparing") == 0 && !value.getString("backup_prepare_failure").isEmpty()) break;
            SystemClock.sleep(100);
        }
        require(value.getInt("backup_prepared") == imageCount+1 && value.getInt("backup_preparing") == 0,
            "native_staging_prepares_selected_originals_without_remote_confirmation");
        try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(queueFile().getPath(), null, 0);
             android.database.Cursor rows = db.rawQuery("SELECT volume,media_id,size,staged_id,staged_manifest,confirmed FROM items WHERE eligible=1", null)) {
            while (rows.moveToNext()) {
                require(rows.getInt(5) == 0, "staged_originals_remain_unconfirmed_until_thinkpad_acknowledges");
                String id = rows.getString(3);
                JSONObject manifest = new JSONObject(rows.getString(4));
                File folder = new File(activity.getFilesDir(), "camera-backup/staged/" + id);
                require(id.matches("[a-f0-9]{64}") && new File(folder, "manifest.json").isFile(), "native_staging_publishes_private_complete_manifest");
                java.security.MessageDigest hash = java.security.MessageDigest.getInstance("SHA-256");
                try (java.io.InputStream input = activity.getContentResolver().openInputStream(android.content.ContentUris.withAppendedId(
                    android.provider.MediaStore.Files.getContentUri(rows.getString(0)), rows.getLong(1)))) {
                    byte[] bytes = new byte[8192]; int count;
                    while ((count = input.read(bytes)) > 0) hash.update(bytes, 0, count);
                }
                StringBuilder digest = new StringBuilder(); for (byte part : hash.digest()) digest.append(String.format("%02x", part & 255));
                require(digest.toString().equals(id), "native_staging_identity_matches_exact_granted_original");
                long bytes = 0;
                for (int i = 0; i < manifest.getJSONArray("chunks").length(); i++) {
                    File chunk = new File(folder, i + ".blob"); bytes += chunk.length()-40;
                    require(chunk.length() >= 40, "native_staging_stores_only_authenticated_encrypted_chunks");
                }
                require(bytes == rows.getLong(2), "native_staging_retains_exact_original_byte_size");
            }
        }
        BackupQueue staged = new BackupQueue(activity, () -> {}, false);
        try {
            require(staged.scan(() -> false) && staged.prepared == imageCount+1 && staged.confirmed == 0,
                "encrypted_staging_survives_rescan_without_claiming_remote_backup");
        } finally { staged.close(); }
        action("back", "", 0);
    }

    // Runs last: the harness forwards this guest's loopback port to its own
    // disposable Daochi node, whose identity matches the recovered pin.
    private void uploadChecks() throws Exception {
        int expected;
        BackupQueue before = new BackupQueue(activity, () -> {}, false);
        try {
            require(before.scan(() -> false) && before.prepared >= 2 && before.confirmed == 0,
                "prepared_originals_wait_for_thinkpad_acknowledgment");
            expected = before.prepared;
        } finally { before.close(); }
        action("upload_backup", "", 0);
        JSONObject value = state();
        long deadline = SystemClock.uptimeMillis() + 240000;
        while (SystemClock.uptimeMillis() < deadline) {
            value = state();
            if (value.getInt("backup_uploading") == 0 && (value.getInt("backup_confirmed") >= expected
                || !value.getString("backup_upload_failure").isEmpty())) break;
            SystemClock.sleep(200);
        }
        require(value.getString("backup_upload_failure").isEmpty(), "pinned_upload_failure_" + value.getString("backup_upload_failure"));
        require(value.getInt("backup_confirmed") == expected && value.getInt("backup_prepared") == 0,
            "pinned_thinkpad_acknowledges_every_encrypted_original");
        String[] staged = new File(activity.getFilesDir(), "camera-backup/staged").list();
        require(staged != null && staged.length == 0, "acknowledged_ciphertext_copies_are_released");
        File device = new File(activity.getFilesDir(), "backup-device");
        require(device.length() == 99, "device_signing_key_is_keystore_sealed");
        BackupQueue after = new BackupQueue(activity, () -> {}, false);
        try {
            require(after.scan(() -> false) && after.confirmed == expected && after.queued == 0 && after.prepared == 0,
                "unchanged_originals_stay_backed_up_after_rescan");
            require(after.upload(() -> false), "repeated_upload_with_nothing_pending_is_a_no_op");
        } finally { after.close(); }
        screenshot("upload");
    }

    private int confirmedCount() throws Exception {
        try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(queueFile().getPath(), null, 0);
             android.database.Cursor rows = db.rawQuery("SELECT COUNT(*) FROM items WHERE confirmed=1", null)) {
            return rows.moveToFirst() ? rows.getInt(0) : -1;
        }
    }

    // The harness published one new camera photo. Photos does not drive this:
    // the OS periodic job discovers, encrypts and uploads it by itself.
    private void backgroundChecks() throws Exception {
        int before = confirmedCount();
        require(before >= 2, "earlier_uploads_remain_confirmed");
        shell("input keyevent KEYCODE_HOME");
        long hidden = SystemClock.uptimeMillis() + 10000;
        while (SystemClock.uptimeMillis() < hidden && PhotosActivity.visible) SystemClock.sleep(100);
        require(!PhotosActivity.visible, "photos_is_off_screen_before_the_periodic_job");
        String forced = shell("cmd jobscheduler run -f " + activity.getPackageName() + " " + BackupQueue.PERIODIC_JOB).trim();
        long deadline = SystemClock.uptimeMillis() + 240000;
        while (SystemClock.uptimeMillis() < deadline && confirmedCount() < before + 1) SystemClock.sleep(500);
        require(confirmedCount() == before + 1, "periodic_job_encrypts_and_backs_up_new_camera_photo_" + forced.replace(' ', '_'));
        String[] staged = new File(activity.getFilesDir(), "camera-backup/staged").list();
        require(staged != null && staged.length == 0, "background_upload_releases_its_ciphertext_copy");
        BackupQueue after = new BackupQueue(activity, () -> {}, false);
        try {
            require(after.scan(() -> false) && after.queued == 0 && after.prepared == 0 && after.confirmed == before + 1,
                "background_backup_survives_rescan_without_duplicates");
        } finally { after.close(); }
        // Settings shows the backed-up count and the manual upload control.
        getContext().startActivity(new Intent(getTargetContext(), PhotosActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        long shown = SystemClock.uptimeMillis() + 10000;
        while (SystemClock.uptimeMillis() < shown && !PhotosActivity.visible) SystemClock.sleep(100);
        require(PhotosActivity.visible, "photos_returns_to_the_foreground");
        SystemClock.sleep(1000);
        settled();
        tap(409, 102);
        for (int i = 0; i < 3; i++) { shell("input swipe 240 860 240 160 400"); SystemClock.sleep(600); }
        screenshot("background");
    }

    private static byte[] readBytes(java.io.InputStream input) throws Exception {
        java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
        byte[] bytes = new byte[8192]; int count;
        while ((count = input.read(bytes)) >= 0) output.write(bytes, 0, count);
        return output.toByteArray();
    }

    private String shell(String command) throws Exception {
        try (ParcelFileDescriptor result = getUiAutomation().executeShellCommand(command);
             FileInputStream input = new FileInputStream(result.getFileDescriptor())) {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] bytes = new byte[8192]; int length;
            while ((length = input.read(bytes)) >= 0) out.write(bytes, 0, length);
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            require("1".equals(shell("getprop ro.kernel.qemu").trim()), "test_owned_emulator");
            require("photos_gallery".equals(shell("getprop ro.boot.qemu.avd_name").trim()), "test_owned_avd");
            activity = (PhotosActivity)startActivitySync(new Intent(Intent.ACTION_MAIN)
                .setClassName(getTargetContext(), "xyz.waozi.photos.PhotosActivity")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            SystemClock.sleep(1000);
            JSONObject value = settled();
            require(activity.backupCryptoStatus == 42, "native_backup_encryption_authenticated_and_retry_safe");
            // This face has 1000-unit CJK advances and 1448 units of pixel
            // height. Its 64-pixel atlas yields 33 logical pixels at size 24.
            require(activity.cjkGlyphWidth >= 32 && activity.cjkGlyphWidth <= 35,
                "native_true_type_cjk_glyph_metrics_width_" + activity.cjkGlyphWidth);
            if (stage.equals("lock")) {
                lockChecks(value);
            } else if (stage.equals("backup")) {
                backupChecks();
            } else if (stage.equals("selected")) {
                selectedChecks();
            } else if (stage.equals("upload")) {
                uploadChecks();
            } else if (stage.equals("background")) {
                backgroundChecks();
            } else if (!stage.equals("full")) {
                require(value.getInt("access") == (stage.equals("partial") ? 4 : 0), "expected_access");
                require(value.getJSONArray("photos").length() == 0 && value.getLong("selected") == -1, "ungranted_photos_excluded");
                require(new File(activity.getCacheDir(), "gallery").list().length == 0, "private_previews_cleared");
                BackupQueue queue = new BackupQueue(activity, () -> {}, false);
                try {
                    require(queue.scan(() -> false) && queue.queued == 0, "ungranted_originals_are_not_camera_candidates");
                    if (queue.folderCount() > 0) require(queue.paused > 0, "revoked_camera_access_retains_paused_queue");
                } finally { queue.close(); }
                screenshot(stage);
            } else {
                require(value.getInt("access") == 3, "full_media_access");
                require(value.getInt("total") == 36 && value.getJSONArray("photos").length() == 30 && value.getInt("more") == 1, "bounded_first_page");
                JSONArray photos = value.getJSONArray("photos");
                for (int i = 0; i < photos.length(); i++) {
                    JSONObject photo = photos.getJSONObject(i);
                    require(new File(photo.getString("image")).length() > 0, "decoded_preview_" + i);
                }
                boolean cameraDate = false;
                for (int i = 0; i < photos.length(); i++) {
                    JSONObject item = photos.getJSONObject(i);
                    if (item.getString("name").equals("写真-00-é.jpg") && item.getString("date").equals("2020-01-01")) cameraDate = true;
                }
                require(!cameraDate, "capture_date_order_with_missing_dates");
                screenshot("gallery");
                String touched = photos.getJSONObject(1).getString("id");
                tap(180, 220);
                value = viewerAt(touched);
                require(value.getLong("selected") == Long.parseLong(touched), "native_thumbnail_touch_opens_viewer");
                swipe(380, 80, 500);
                value = viewerAt(photos.getJSONObject(2).getString("id"));
                require(value.getLong("selected") == Long.parseLong(photos.getJSONObject(2).getString("id")), "native_swipe_next");
                swipe(80, 380, 500);
                value = viewerAt(touched);
                require(value.getLong("selected") == Long.parseLong(touched), "native_swipe_previous");
                int fitPixel = previewPixel();
                tap(240, 500, 30); tap(240, 500, 30);
                SystemClock.sleep(500);
                require(previewPixel() != fitPixel, "native_double_tap_zoom");
                screenshot("viewer-zoom");
                swipe(380, 80, 500);
                require(settled().getLong("selected") == Long.parseLong(touched), "zoomed_pan_does_not_navigate");
                tap(240, 500, 30); tap(240, 500, 30);
                SystemClock.sleep(500);
                require(previewPixel() == fitPixel, "native_double_tap_fit");
                tap(54, 185);
                value = settled();
                require(value.getLong("selected") == -1 && value.getInt("total") == 36, "native_touch_returns_to_gallery");
                value = action("page", "", 1);
                require(value.getJSONArray("photos").length() == 6 && value.getInt("page") == 1 && value.getInt("more") == 0, "second_page");
                for (int i = 0; i < value.getJSONArray("photos").length(); i++) {
                    JSONObject item = value.getJSONArray("photos").getJSONObject(i);
                    if (item.getString("name").equals("写真-00-é.jpg") && item.getString("date").equals("2020-01-01")) cameraDate = true;
                }
                require(cameraDate, "camera_capture_date");
                value = action("view", "albums", 0);
                require(value.getJSONArray("albums").length() == 2, "device_folders");
                String folder = "";
                for (int i = 0; i < value.getJSONArray("albums").length(); i++) {
                    JSONObject item = value.getJSONArray("albums").getJSONObject(i);
                    if (item.getInt("count") == 35) folder = item.getString("id");
                }
                require(!folder.isEmpty(), "photo_folder_count");
                screenshot("albums");
                value = action("album", folder, 0);
                require(value.getInt("total") == 35 && value.getString("album").equals(folder), "folder_filter");
                String first = value.getJSONArray("photos").getJSONObject(0).getString("id");
                String second = value.getJSONArray("photos").getJSONObject(1).getString("id");
                value = action("open", first, 0);
                require(value.getJSONArray("photos").length() == 1 && value.getString("selected").equals(first), "native_viewer");
                require(value.getJSONArray("photos").getJSONObject(0).getString("name").contains("写真"), "unicode_metadata");
                SystemClock.sleep(500);
                require(new String(activity.nativeViewerName, StandardCharsets.UTF_8).contains("写真"), "native_unicode_metadata");
                screenshot("viewer");
                value = action("next", "", 0);
                require(value.getString("selected").equals(second), "next_in_folder");
                value = action("previous", "", 0);
                require(value.getString("selected").equals(first), "previous_in_folder");
                action("share", "", 0);
                SystemClock.sleep(1500);
                require(state().getJSONArray("photos").length() == 0, "chooser_pauses_private_view");
                screenshot("share");
                getUiAutomation().performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK);
                value = galleryTotal(35);
                require(value.getLong("selected") == -1 && value.getInt("total") == 35, "resume_requeries_provider");
                SystemClock.sleep(1500);
                require(!activity.isDestroyed() && !activity.isFinishing() && state().getInt("total") == 35,
                    "resume_keeps_native_gallery_alive_after_private_previews_are_cleared");
                value = action("back", "", 0);
                require(value.getString("view").equals("albums") && value.getString("album").isEmpty(), "return_to_device_folders");
                value = action("view", "videos", 0);
                require(value.getInt("total") == 1 && value.getJSONArray("photos").getJSONObject(0).getInt("video") == 1, "video_filter");
                screenshot("videos");
                value = action("view", "photos", 0);
                first = "";
                for (int i = 0; i < value.getJSONArray("photos").length(); i++) {
                    JSONObject item = value.getJSONArray("photos").getJSONObject(i);
                    if (item.getInt("video") == 0) { first = item.getString("id"); break; }
                }
                require(!first.isEmpty(), "revocation_fixture_is_a_photo_and_retains_video_folder");
                action("open", first, 0);
                shell("content delete --uri content://media/external/file/" + first);
                SystemClock.sleep(1500);
                value = settled();
                require(value.getJSONArray("photos").length() == 0 && value.getLong("selected") == -1, "removed_viewer_access_revoked");
                value = action("refresh", "", 0);
                require(value.getInt("total") == 35, "live_provider_updates");
                screenshot("refreshed");
            }
            try (FileOutputStream output = new FileOutputStream(new File(activity.getFilesDir(), stage + "-checks.json"))) {
                output.write(checks.toString(2).getBytes(StandardCharsets.UTF_8));
            }
            result.putString("checks", checks.toString());
            finish(ActivityResult.OK, result);
        } catch (Throwable error) {
            result.putString("error", error.toString());
            try {
                result.putString("gallery_state", state().toString());
                result.putString("native_name", new String(activity.nativeViewerName, StandardCharsets.UTF_8));
                screenshot("probe-failure");
            } catch (Throwable ignored) { }
            finish(ActivityResult.FAILED, result);
        }
    }

    private static final class ActivityResult {
        static final int OK = -1, FAILED = 0;
    }
}
