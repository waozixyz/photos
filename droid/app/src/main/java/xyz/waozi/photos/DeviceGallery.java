package xyz.waozi.photos;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.ClipData;
import android.content.ContentUris;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.ContentObserver;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.ImageDecoder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.util.Size;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Android's permission-scoped media provider and codecs; the UI lives in Zi. */
final class DeviceGallery {
    private final PhotosActivity activity;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final File cache;
    private final ContentObserver observer;
    private final DeviceLock lock;
    private final DeviceBackup backup;
    private final BackupQueue queue;
    private volatile byte[] state;
    private volatile boolean active;
    private volatile boolean closed;
    private long generation;
    private int permission;
    private int page;
    // Items shown from the current page, grown by scrolling ("more") up to 480.
    private int limit = 30;
    // Thumbnails stay cached while the view, folder, page and access are unchanged.
    private String cacheKey = "";
    private String mode = "photos";
    private String album = "";
    private long selected = -1;
    private boolean selectedVideo;
    private long lastValidation;
    private String[] visibleIds = new String[0];

    // MediaStore accepts column sort orders, not a computed fallback date.
    // Merge its two sorted cursors without retaining a whole device library.
    private final class Rows implements AutoCloseable {
        private Cursor captured, added, current;
        private boolean capturedReady, addedReady, first = true;
        private final int captureColumn, addedColumn;

        Rows(String[] columns, String selection, String[] arguments) {
            String date = MediaStore.Images.ImageColumns.DATE_TAKEN;
            captureColumn = java.util.Arrays.asList(columns).indexOf(date);
            addedColumn = java.util.Arrays.asList(columns).indexOf("date_added");
            try {
                captured = activity.getContentResolver().query(MediaStore.Files.getContentUri("external"), columns,
                    selection + " AND " + date + " IS NOT NULL", arguments, date + " DESC, _id DESC");
                added = activity.getContentResolver().query(MediaStore.Files.getContentUri("external"), columns,
                    selection + " AND " + date + " IS NULL", arguments, "date_added DESC, _id DESC");
            } catch (RuntimeException error) { close(); throw error; }
        }

        int count() { return (captured == null ? 0 : captured.getCount()) + (added == null ? 0 : added.getCount()); }
        Cursor cursor() { return current; }
        long date(Cursor row) {
            return row.isNull(captureColumn) ? row.getLong(addedColumn) * 1000 : row.getLong(captureColumn);
        }
        boolean next() {
            if (first) {
                first = false;
                capturedReady = captured != null && captured.moveToFirst();
                addedReady = added != null && added.moveToFirst();
            } else if (current == captured) capturedReady = captured.moveToNext();
            else if (current == added) addedReady = added.moveToNext();
            if (!capturedReady && !addedReady) { current = null; return false; }
            if (!capturedReady) current = added;
            else if (!addedReady) current = captured;
            else {
                long captureDate = date(captured), addedDate = date(added);
                current = captureDate > addedDate || (captureDate == addedDate && captured.getLong(0) > added.getLong(0)) ? captured : added;
            }
            return true;
        }
        @Override public void close() {
            if (captured != null) captured.close();
            if (added != null) added.close();
        }
    }

    DeviceGallery(PhotosActivity activity) {
        this.activity = activity;
        cache = new File(activity.getCacheDir(), "gallery");
        cache.mkdirs();
        permission = access();
        lock = new DeviceLock(activity, this::refresh);
        backup = new DeviceBackup(activity, () -> !closed && active && lock.allowed(), this::refresh);
        queue = new BackupQueue(activity, () -> main.post(this::refresh));
        state = empty("Choose photo access to browse this device", false);
        observer = new ContentObserver(main) {
            @Override public void onChange(boolean selfChange) { queue.requestScan(); refresh(); }
        };
        activity.getContentResolver().registerContentObserver(
            MediaStore.Files.getContentUri("external"), true, observer);
    }

    private int access() {
        if (Build.VERSION.SDK_INT >= 33) {
            int result = 0;
            if (activity.checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) == PackageManager.PERMISSION_GRANTED) result |= 1;
            if (activity.checkSelfPermission(Manifest.permission.READ_MEDIA_VIDEO) == PackageManager.PERMISSION_GRANTED) result |= 2;
            if (Build.VERSION.SDK_INT >= 34 && activity.checkSelfPermission(Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED) == PackageManager.PERMISSION_GRANTED) result |= 4;
            return result;
        }
        return activity.checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED ? 3 : 0;
    }

    synchronized void resume() { active = true; queue.requestScan(); refresh(); }
    synchronized void pause() {
        if (closed) return;
        active = false; generation++; selected = -1;
        lock.pause();
        visibleIds = new String[0];
        state = empty("Photos is paused", false);
        executor.execute(this::clearCache);
    }
    synchronized void close() {
        closed = true; active = false; generation++; selected = -1;
        lock.close();
        backup.close();
        queue.close();
        state = empty("Photos is closed", false);
        activity.getContentResolver().unregisterContentObserver(observer);
        executor.shutdownNow();
        clearCache();
    }

    synchronized byte[] state() {
        if (permission != access()) refresh();
        // Selected access can change without changing the permission bits.
        // Revalidate cached rows against MediaStore, including a held viewer.
        long now = SystemClock.uptimeMillis();
        if (active && visibleIds.length > 0 && now - lastValidation >= 1000) {
            lastValidation = now;
            StringBuilder selection = new StringBuilder("media_type IN (1,3) AND _id IN (");
            for (int i = 0; i < visibleIds.length; i++) selection.append(i == 0 ? "?" : ",?");
            selection.append(')');
            try (Cursor rows = activity.getContentResolver().query(
                MediaStore.Files.getContentUri("external"), new String[]{"_id"}, selection.toString(), visibleIds, null)) {
                if (rows == null || rows.getCount() != visibleIds.length) refresh();
            } catch (Exception error) { refresh(); }
        }
        return state;
    }

    synchronized void action(String request) {
        if (closed) return;
        try {
            JSONObject input = new JSONObject(request);
            String action = input.optString("action");
            if (action.equals("unlock") || action.equals("enable_lock") || action.equals("disable_lock") || action.equals("lock") ||
                action.equals("hide_content") || action.equals("device_security")) {
                activity.runOnUiThread(() -> {
                    synchronized (DeviceGallery.this) { if (!active || closed) return; }
                    lock.action(action);
                });
                return;
            }
            if (!active || !lock.allowed()) return;
            if (action.equals("choose_recovery")) {
                activity.runOnUiThread(backup::choose);
            } else if (action.equals("backup_folder")) {
                if (backup.ready && !album.isEmpty()) queue.toggle(album);
            } else if (action.equals("scan_backup")) {
                if (backup.ready) queue.requestScan();
            } else if (action.equals("prepare_backup")) {
                if (backup.ready) queue.prepare(backup, activity);
            } else if (action.equals("upload_backup")) {
                if (backup.ready) queue.requestUpload();
            } else if (action.equals("stop_backup_queue")) {
                queue.stop();
            } else if (action.equals("access")) {
                activity.runOnUiThread(() -> {
                    String[] permissions;
                    if (Build.VERSION.SDK_INT >= 34) permissions = new String[]{Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED};
                    else if (Build.VERSION.SDK_INT >= 33) permissions = new String[]{Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO};
                    else permissions = new String[]{Manifest.permission.READ_EXTERNAL_STORAGE};
                    activity.requestPermissions(permissions, 301);
                });
            } else if (action.equals("settings")) {
                activity.runOnUiThread(() -> activity.startActivity(new Intent(
                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + activity.getPackageName()))));
            } else if (action.equals("view")) {
                String next = input.optString("view");
                if (!next.equals("photos") && !next.equals("videos") && !next.equals("albums")) return;
                mode = next; album = ""; selected = -1; page = 0; limit = 30; refresh();
            } else if (action.equals("album")) {
                String id = input.optString("id");
                if (!id.matches("-?[0-9]{1,20}")) return;
                album = id; mode = "photos"; selected = -1; page = 0; limit = 30; refresh();
            } else if (action.equals("page")) {
                page = Math.max(0, Math.min(100000, input.optInt("page"))); selected = -1; limit = 30; refresh();
            } else if (action.equals("more")) {
                // Continuous scrolling extends the loaded list in bounded steps.
                if (selected >= 0 || mode.equals("albums") || limit >= 480) return;
                limit = Math.min(limit + 60, 480); refresh(true);
            } else if (action.equals("open")) {
                long id = input.optLong("id", -1);
                if (id < 0 || !allowed(id)) return;
                selected = id; selectedVideo = isVideo(id); refresh();
            } else if (action.equals("play")) {
                final long id = selected;
                if (id < 0 || !selectedVideo || !allowed(id)) return;
                final Uri uri = ContentUris.withAppendedId(MediaStore.Video.Media.getContentUri("external"), id);
                activity.runOnUiThread(() -> {
                    if (!allowed(id)) { refresh(); return; }
                    try {
                        activity.startActivity(new Intent(Intent.ACTION_VIEW).setDataAndType(uri, "video/*")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
                    } catch (android.content.ActivityNotFoundException error) {
                        synchronized (DeviceGallery.this) { state = empty("Install a video player to play this video", false); }
                    }
                });
            } else if (action.equals("previous") || action.equals("next")) {
                final long revision = generation, id = selected;
                final int direction = action.equals("next") ? 1 : -1;
                final String currentMode = mode, currentAlbum = album;
                if (id < 0 || !allowed(id)) return;
                executor.execute(() -> navigate(revision, id, direction, currentMode, currentAlbum));
            } else if (action.equals("share")) {
                final long id = selected;
                final boolean video = selectedVideo;
                if (id < 0 || !allowed(id)) return;
                final Uri uri = ContentUris.withAppendedId(video ? MediaStore.Video.Media.getContentUri("external") : MediaStore.Images.Media.getContentUri("external"), id);
                activity.runOnUiThread(() -> {
                    if (!allowed(id)) { refresh(); return; }
                    Intent intent = new Intent(Intent.ACTION_SEND).setType(video ? "video/*" : "image/*")
                        .putExtra(Intent.EXTRA_STREAM, uri)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    intent.setClipData(ClipData.newUri(activity.getContentResolver(), "Photo", uri));
                    activity.startActivity(Intent.createChooser(intent, "Share photo"));
                });
            } else if (action.equals("back")) {
                if (selected < 0 && !album.isEmpty()) { album = ""; mode = "albums"; page = 0; limit = 30; }
                selected = -1; refresh();
            } else if (action.equals("refresh")) refresh();
        } catch (Exception ignored) { }
    }

    private boolean allowed(long id) {
        if (!active || !lock.allowed() || access() == 0) return false;
        try (Cursor cursor = activity.getContentResolver().query(
            MediaStore.Files.getContentUri("external"), new String[]{"_id"}, "_id=? AND media_type IN (1,3)",
            new String[]{Long.toString(id)}, null)) {
            return cursor != null && cursor.moveToFirst();
        } catch (Exception error) { return false; }
    }

    private boolean isVideo(long id) {
        try (Cursor cursor = activity.getContentResolver().query(
            MediaStore.Files.getContentUri("external"), new String[]{"media_type"}, "_id=?",
            new String[]{Long.toString(id)}, null)) {
            return cursor != null && cursor.moveToFirst() && cursor.getInt(0) == 3;
        } catch (Exception error) { return false; }
    }

    private void navigate(long revision, long id, int direction, String currentMode, String currentAlbum) {
        if (!current(revision)) return;
        String selection = "media_type IN (1,3)";
        if (Build.VERSION.SDK_INT >= 29) selection += " AND is_pending=0";
        if (Build.VERSION.SDK_INT >= 30) selection += " AND is_trashed=0";
        if (currentMode.equals("videos")) selection += " AND media_type=3";
        String[] args = new String[0];
        if (!currentAlbum.isEmpty()) { selection += " AND bucket_id=?"; args = new String[]{currentAlbum}; }
        try (Rows rows = new Rows(new String[]{"_id", "media_type", MediaStore.Images.ImageColumns.DATE_TAKEN, "date_added"}, selection, args)) {
            long previous = -1; boolean previousVideo = false, found = false; int index = 0;
            while (rows.next() && current(revision)) {
                Cursor row = rows.cursor();
                long next = row.getLong(0); boolean nextVideo = row.getInt(1) == 3;
                boolean target = direction > 0 ? found : next == id && previous >= 0;
                if (target) {
                synchronized (this) {
                    if (!current(revision) || selected != id) return;
                    selected = direction > 0 ? next : previous;
                    selectedVideo = direction > 0 ? nextVideo : previousVideo;
                    int position = direction > 0 ? index : index - 1;
                    if (position < page * 30 || position >= page * 30 + limit) { page = position / 30; limit = 30; }
                    refresh();
                }
                return;
                }
                if (next == id) found = true;
                previous = next; previousVideo = nextVideo; index++;
            }
        } catch (Exception error) { synchronized (this) { if (current(revision)) refresh(); } }
    }

    synchronized void refresh() { refresh(false); }

    // Appending keeps the current photos visible until the longer list loads.
    synchronized void refresh(boolean keep) {
        if (closed) return;
        permission = access();
        visibleIds = new String[0];
        long revision = ++generation;
        int currentPage = page, currentLimit = limit;
        String currentMode = mode, currentAlbum = album;
        long currentSelected = selected;
        if (!keep || permission == 0 || !lock.allowed())
            state = empty(lock.locked ? "Photos is locked" : !lock.message.isEmpty() ? lock.message : permission == 0 ? "Choose photo access to browse this device" : "Loading this device",
                active && lock.allowed() && permission != 0);
        executor.execute(() -> {
            if (!current(revision)) { if (generation == revision) clearCache(); return; }
            load(revision, currentPage, currentLimit, currentMode, currentAlbum, currentSelected);
        });
    }

    private byte[] empty(String status, boolean loading) {
        try {
            return security(new JSONObject()).put("generation", generation).put("access", permission)
                .put("status", status).put("loading", loading ? 1 : 0).put("page", page)
                .put("view", mode).put("album", album).put("total", 0).put("more", 0)
                .put("photos", new JSONArray()).put("albums", new JSONArray())
                .put("selected", -1).toString().getBytes(StandardCharsets.UTF_8);
        } catch (Exception error) { return "{}".getBytes(StandardCharsets.UTF_8); }
    }

    private synchronized boolean current(long revision) {
        return !closed && active && lock.allowed() && generation == revision && permission != 0 && permission == access();
    }

    synchronized void authenticationResult(int resultCode) { lock.result(resultCode); }
    synchronized void backupResult(int resultCode, Intent data) { backup.result(resultCode, data); }
    synchronized void permissionResult() { queue.requestScan(); refresh(); }
    void restoreBackup(byte[] password) { backup.restore(password); }
    boolean backupStageCurrent() { return lock.allowed() && queue.stageCurrent(); }

    private JSONObject security(JSONObject result) throws org.json.JSONException {
        return result.put("typeface", new File(activity.getFilesDir(), "ui-typeface.ttf").getPath())
            .put("lock_enabled", lock.enabled ? 1 : 0).put("locked", lock.locked ? 1 : 0)
            .put("hide_content", lock.hidden ? 1 : 0).put("auth_pending", lock.pending ? 1 : 0).put("device_secure", lock.secure() ? 1 : 0)
            .put("security_status", lock.message)
            .put("backup_configured", backup.configured ? 1 : 0).put("backup_ready", backup.ready ? 1 : 0)
            .put("backup_pending", backup.pending ? 1 : 0).put("recovery_selected", backup.recoverySelected ? 1 : 0)
            .put("backup_status", backup.message)
            .put("backup_queue_status", lock.allowed() ? queue.message : "")
            .put("backup_folder_selected", lock.allowed() && queue.folderSelected(album) ? 1 : 0)
            .put("backup_folder_count", lock.allowed() ? queue.folderCount() : 0)
            .put("backup_queued", lock.allowed() ? queue.queued : 0)
            .put("backup_paused", lock.allowed() ? queue.paused : 0)
            .put("backup_prepared", lock.allowed() ? queue.prepared : 0)
            .put("backup_prepare_failure", BuildConfig.DEBUG && lock.allowed() ? queue.prepareFailure : "")
            .put("backup_preparing", lock.allowed() && queue.preparing ? 1 : 0)
            .put("backup_uploading", lock.allowed() && queue.uploading ? 1 : 0)
            .put("backup_confirmed", lock.allowed() ? queue.confirmed : 0)
            .put("backup_upload_failure", BuildConfig.DEBUG && lock.allowed() ? queue.uploadFailure : "")
            .put("backup_scanning", lock.allowed() && queue.scanning ? 1 : 0);
    }

    private void clearCache() {
        synchronized (this) { cacheKey = ""; }
        File[] files = cache.listFiles();
        if (files != null) for (File file : files) if (file.isFile()) file.delete();
    }

    // Full-size viewer images are never reused across loads.
    private void clearViews() {
        File[] files = cache.listFiles();
        if (files != null) for (File file : files) if (file.isFile() && file.getName().endsWith("-view.png")) file.delete();
    }

    private File image(Uri uri, long revision, long id, boolean large, boolean video) throws Exception {
        // A thumbnail decoded for this view is reused while it stays current;
        // the row itself came from this revision's permission-scoped query.
        File thumbnail = new File(cache, "t-" + id + ".png");
        if (!large && thumbnail.isFile() && current(revision)) return thumbnail;
        Bitmap bitmap;
        if (large && !video) {
            bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(activity.getContentResolver(), uri), (decoder, info, source) -> {
                Size size = info.getSize();
                int longest = Math.max(size.getWidth(), size.getHeight());
                if (longest > 2048) decoder.setTargetSampleSize((longest + 2047) / 2048);
                decoder.setAllocator(ImageDecoder.ALLOCATOR_SOFTWARE);
            });
        } else if (Build.VERSION.SDK_INT >= 29) bitmap = activity.getContentResolver().loadThumbnail(uri, new Size(256, 256), null);
        else if (video) bitmap = MediaStore.Video.Thumbnails.getThumbnail(activity.getContentResolver(), id, MediaStore.Video.Thumbnails.MINI_KIND, null);
        else bitmap = MediaStore.Images.Thumbnails.getThumbnail(activity.getContentResolver(), id, MediaStore.Images.Thumbnails.MINI_KIND, null);
        if (bitmap == null) throw new java.io.IOException("No preview available");
        File file = large ? new File(cache, revision + "-" + id + "-view.png") : thumbnail;
        try {
            if (!current(revision) || !allowed(id)) throw new SecurityException();
            try (FileOutputStream output = new FileOutputStream(file)) {
                if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) throw new java.io.IOException();
            }
            if (!current(revision) || !allowed(id)) { file.delete(); throw new SecurityException(); }
            return file;
        } finally { bitmap.recycle(); }
    }

    private void load(long revision, int currentPage, int currentLimit, String currentMode, String currentAlbum, long currentSelected) {
        if (!current(revision)) return;
        String key = currentMode + "\n" + currentAlbum + "\n" + permission + "\n" + currentPage;
        boolean reuse;
        synchronized (this) { reuse = key.equals(cacheKey); }
        if (reuse) clearViews(); else { clearCache(); synchronized (this) { cacheKey = key; } }
        try {
            ContentResolver resolver = activity.getContentResolver();
            JSONArray photos = new JSONArray(), albums = new JSONArray();
            java.util.LinkedHashMap<String, JSONObject> folders = new java.util.LinkedHashMap<>();
            String selection = "media_type IN (1,3)";
            if (Build.VERSION.SDK_INT >= 29) selection += " AND is_pending=0";
            if (Build.VERSION.SDK_INT >= 30) selection += " AND is_trashed=0";
            java.util.ArrayList<String> arguments = new java.util.ArrayList<>();
            if (currentMode.equals("videos")) selection += " AND media_type=3";
            if (!currentAlbum.isEmpty()) { selection += " AND bucket_id=?"; arguments.add(currentAlbum); }
            if (currentSelected >= 0) { selection += " AND _id=?"; arguments.add(Long.toString(currentSelected)); }
            String[] columns = {"_id", "_display_name", "media_type", MediaStore.Images.ImageColumns.DATE_TAKEN, "date_added", "bucket_id", "bucket_display_name", "_size", "width", "height"};
            int total = 0;
            // Capture dates group by the phone's own calendar day, as people expect.
            SimpleDateFormat day = new SimpleDateFormat("yyyy-MM-dd", Locale.ROOT); day.setTimeZone(TimeZone.getDefault());
            long now = System.currentTimeMillis();
            String today = day.format(new Date(now)), yesterday = day.format(new Date(now - 86400000L));
            try (Rows rows = new Rows(columns, selection, arguments.toArray(new String[0]))) {
                    total = rows.count();
                    if (currentMode.equals("albums")) {
                        while (rows.next() && current(revision)) {
                            Cursor cursor = rows.cursor();
                            String id = cursor.getString(5), name = cursor.getString(6);
                            if (id == null) continue;
                            JSONObject folder = folders.get(id);
                            if (folder == null) {
                                // Rows arrive newest first: the first item is the folder's cover.
                                folder = new JSONObject().put("id", id).put("name", name == null ? "Photos" : name).put("count", 0)
                                    .put("cover_id", cursor.getLong(0)).put("cover_video", cursor.getInt(2) == 3);
                                folders.put(id, folder);
                            }
                            folder.put("count", folder.getInt("count") + 1);
                        }
                        int start = currentPage * 30, index = 0;
                        for (JSONObject folder : folders.values()) {
                            if (index >= start && index < start + 30 && current(revision)) {
                                long cover = folder.getLong("cover_id"); boolean video = folder.getBoolean("cover_video");
                                Uri uri = ContentUris.withAppendedId(video ? MediaStore.Video.Media.getContentUri("external") : MediaStore.Images.Media.getContentUri("external"), cover);
                                try { folder.put("cover", image(uri, revision, cover, false, video).getAbsolutePath()); }
                                catch (Exception error) { folder.put("cover", ""); }
                                folder.remove("cover_id"); folder.remove("cover_video");
                                albums.put(folder);
                            }
                            index++;
                        }
                        total = folders.size();
                    } else {
                        int start = currentSelected >= 0 ? 0 : currentPage * 30;
                        int index = 0;
                        while (photos.length() < (currentSelected >= 0 ? 1 : currentLimit) && rows.next()) {
                            if (!current(revision)) return;
                            if (index++ < start) continue;
                            Cursor cursor = rows.cursor();
                            long id = cursor.getLong(0); boolean video = cursor.getInt(2) == 3;
                            Uri uri = ContentUris.withAppendedId(video ? MediaStore.Video.Media.getContentUri("external") : MediaStore.Images.Media.getContentUri("external"), id);
                            long taken = rows.date(cursor);
                            JSONObject photo = new JSONObject().put("id", Long.toString(id)).put("name", cursor.getString(1))
                                .put("date", day.format(new Date(taken))).put("video", video ? 1 : 0)
                                .put("folder", cursor.getString(6)).put("size", cursor.getLong(7))
                                .put("width", cursor.getInt(8)).put("height", cursor.getInt(9));
                            try { photo.put("image", image(uri, revision, id, currentSelected >= 0, video).getAbsolutePath()); }
                            catch (Exception error) { photo.put("image", ""); }
                            photos.put(photo);
                        }
                    }
            }
            synchronized (this) {
                if (!current(revision)) return;
                if (currentSelected >= 0 && !allowed(currentSelected)) { selected = -1; state = empty("Photo access changed; refresh your gallery", false); return; }
                visibleIds = new String[photos.length()];
                for (int i = 0; i < photos.length(); i++) visibleIds[i] = photos.getJSONObject(i).getString("id");
                state = security(new JSONObject()).put("generation", revision).put("access", permission)
                    .put("status", (permission & 3) == 3 ? "On this device" : (permission & 4) != 0 ? "Selected photos and videos" : "On this device")
                    .put("loading", 0).put("page", currentPage).put("view", currentMode).put("album", currentAlbum)
                    .put("total", total).put("today", today).put("yesterday", yesterday)
                    .put("more", currentSelected < 0 && (currentMode.equals("albums") ? (currentPage + 1) * 30 : currentPage * 30 + photos.length()) < total ? 1 : 0)
                    .put("photos", photos).put("albums", albums).put("selected", currentSelected)
                    .toString().getBytes(StandardCharsets.UTF_8);
            }
        } catch (Exception error) {
            synchronized (this) { if (current(revision)) state = empty("Photo access changed; choose access or refresh", false); }
        }
    }
}
