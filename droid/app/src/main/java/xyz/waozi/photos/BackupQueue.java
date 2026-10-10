package xyz.waozi.photos;

import android.Manifest;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.ContentValues;
import android.content.Context;
import android.content.ContentUris;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.Build;
import android.os.ParcelFileDescriptor;
import android.graphics.Bitmap;
import android.util.Size;
import android.provider.MediaStore;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.BooleanSupplier;

/** Platform provider, durable storage and job adapter. Eligibility is Ziran.
 * No original is changed. An item counts as backed up only after the pinned
 * ThinkPad node acknowledges its encrypted record and holds every chunk. */
final class BackupQueue {
    private static final Object STORAGE = new Object();
    private static final ExecutorService IO = Executors.newSingleThreadExecutor();
    private static final ExecutorService NETWORK = Executors.newSingleThreadExecutor();
    static final int PERIODIC_JOB = 3381;
    private final Context context;
    private final Runnable changed;
    private volatile boolean closed;
    private boolean busy, again;
    private volatile Set<String> selected = new HashSet<>();
    volatile boolean scanning;
    volatile int queued, paused, confirmed, prepared;
    volatile boolean preparing, uploading;
    volatile String uploadFailure = "";
    volatile String prepareFailure = "";
    private volatile boolean prepareCanceled;
    private volatile Original staging;
    volatile String message = "Choose device folders to queue for ThinkPad backup";

    BackupQueue(Context context, Runnable changed) {
        this(context, changed, true);
    }

    BackupQueue(Context context, Runnable changed, boolean start) {
        this.context = context.getApplicationContext(); this.changed = changed;
        if (start) requestScan();
    }

    static int access(Context context) {
        if (Build.VERSION.SDK_INT < 33) return granted(context, Manifest.permission.READ_EXTERNAL_STORAGE) ? 3 : 0;
        int value = (granted(context, Manifest.permission.READ_MEDIA_IMAGES) ? 1 : 0)
            | (granted(context, Manifest.permission.READ_MEDIA_VIDEO) ? 2 : 0);
        if (Build.VERSION.SDK_INT >= 34 && granted(context, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED)) value |= 4;
        return value;
    }

    private static boolean granted(Context context, String name) {
        return context.checkSelfPermission(name) == PackageManager.PERMISSION_GRANTED;
    }

    private File privateDirectory(String name) {
        try {
            // Android may alias its trusted files directory. App-owned path
            // components must be checked before any directory is created.
            File directory = new File(context.getFilesDir().getCanonicalFile(), name);
            if (!directory.getCanonicalFile().equals(directory.getAbsoluteFile())) throw new IllegalStateException();
            if ((!directory.isDirectory() && !directory.mkdirs())
                || !directory.getCanonicalFile().equals(directory.getAbsoluteFile())) throw new IllegalStateException();
            android.system.Os.chmod(directory.getPath(), 0700);
            return directory;
        } catch (java.io.IOException | android.system.ErrnoException error) {
            throw new IllegalStateException();
        }
    }

    private SQLiteDatabase open() {
        File directory = privateDirectory("camera-backup");
        File queue = new File(directory, "queue.sqlite");
        try {
            if (!queue.getCanonicalFile().equals(queue.getAbsoluteFile())) throw new IllegalStateException();
        } catch (java.io.IOException error) {
            throw new IllegalStateException();
        }
        SQLiteDatabase db = SQLiteDatabase.openOrCreateDatabase(queue, null);
        db.execSQL("CREATE TABLE IF NOT EXISTS folders(bucket TEXT PRIMARY KEY, selected INTEGER NOT NULL CHECK(selected IN (0,1)))");
        db.execSQL("CREATE TABLE IF NOT EXISTS items(volume TEXT NOT NULL, media_id INTEGER NOT NULL, bucket TEXT NOT NULL, "
            + "name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL, generation INTEGER NOT NULL, "
            + "taken INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, media_type INTEGER NOT NULL, "
            + "eligible INTEGER NOT NULL DEFAULT 0 CHECK(eligible IN (0,1)), confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)), "
            + "PRIMARY KEY(volume,media_id))");
        Set<String> columns = new HashSet<>();
        try (Cursor rows = db.rawQuery("PRAGMA table_info(items)", null)) {
            while (rows.moveToNext()) columns.add(rows.getString(1));
        }
        if (!columns.contains("staged_id")) db.execSQL("ALTER TABLE items ADD COLUMN staged_id TEXT NOT NULL DEFAULT ''");
        if (!columns.contains("staged_manifest")) db.execSQL("ALTER TABLE items ADD COLUMN staged_manifest TEXT NOT NULL DEFAULT ''");
        db.execSQL("CREATE TABLE IF NOT EXISTS node(key TEXT PRIMARY KEY, value INTEGER NOT NULL)");
        return db;
    }

    synchronized void requestScan() {
        if (closed) return;
        if (busy) { again = true; return; }
        busy = true; scanning = true;
        IO.execute(() -> {
            try { scan(() -> closed); }
            finally {
                synchronized (BackupQueue.this) { busy = false; scanning = false; }
                notifyChanged();
                synchronized (BackupQueue.this) { if (again) { again = false; requestScan(); } }
            }
        });
    }

    void toggle(String bucket) {
        if (closed || !bucket.matches("-?[0-9]{1,20}")) return;
        prepareCanceled = true;
        IO.execute(() -> {
            if (closed) return;
            try {
                // Only a granted, real device folder can be newly selected.
                try (Cursor visible = context.getContentResolver().query(MediaStore.Files.getContentUri("external"),
                    new String[]{"_id"}, "bucket_id=? AND media_type IN (1,3)", new String[]{bucket}, null)) {
                    if (!selected.contains(bucket) && (access(context) == 0 || visible == null || !visible.moveToFirst())) return;
                }
                synchronized (STORAGE) {
                    try (SQLiteDatabase db = open()) {
                        boolean current;
                        try (Cursor row = db.rawQuery("SELECT selected FROM folders WHERE bucket=?", new String[]{bucket})) {
                            current = row.moveToFirst() && row.getInt(0) == 1;
                        }
                        ContentValues value = new ContentValues(); value.put("bucket", bucket); value.put("selected", current ? 0 : 1);
                        db.insertWithOnConflict("folders", null, value, SQLiteDatabase.CONFLICT_REPLACE);
                    }
                }
                scan(() -> closed);
            } catch (Exception error) { message = "Could not update backup folders; original files were preserved"; }
            notifyChanged();
        });
    }

    void stop() {
        prepareCanceled = true;
        IO.execute(() -> {
            if (closed) return;
            try {
                synchronized (STORAGE) {
                    try (SQLiteDatabase db = open()) {
                        db.beginTransaction();
                        try {
                            db.execSQL("UPDATE folders SET selected=0");
                            db.execSQL("UPDATE items SET eligible=0");
                            db.setTransactionSuccessful();
                        } finally { db.endTransaction(); }
                        snapshot(db);
                    }
                }
                scheduler().cancel(PERIODIC_JOB);
            } catch (Exception error) { message = "Could not stop the backup queue; originals were preserved"; }
            notifyChanged();
        });
    }

    // Called by the OS job on its own worker, with its cancellation signal.
    boolean scan(BooleanSupplier canceled) {
        synchronized (STORAGE) {
            try (SQLiteDatabase db = open()) {
                // Clear eligibility before querying. Failed, interrupted or revoked
                // provider access must leave every old candidate paused.
                db.execSQL("UPDATE items SET eligible=0");
                snapshot(db);
                if (selected.isEmpty()) { scheduler().cancel(PERIODIC_JOB); return true; }
                schedule();
                final int permission = access(context);
                if (permission == 0) { message = "Photo access is required; queued originals are paused"; return true; }
                String[] columns = Build.VERSION.SDK_INT >= 30
                    ? new String[]{"_id", "bucket_id", "_display_name", "mime_type", "_size", "date_modified", "date_added", "generation_modified", "datetaken", "width", "height", "media_type", "is_pending", "is_trashed", "volume_name"}
                    : Build.VERSION.SDK_INT >= 29
                    ? new String[]{"_id", "bucket_id", "_display_name", "mime_type", "_size", "date_modified", "date_added", "datetaken", "width", "height", "media_type", "is_pending", "volume_name"}
                    : new String[]{"_id", "bucket_id", "_display_name", "mime_type", "_size", "date_modified", "date_added", "datetaken", "width", "height", "media_type"};
                db.beginTransaction();
                try (Cursor rows = context.getContentResolver().query(MediaStore.Files.getContentUri("external"), columns,
                    "media_type IN (1,3)", null, "_id ASC")) {
                    if (rows == null) throw new IllegalStateException();
                    while (rows.moveToNext()) {
                        if (canceled.getAsBoolean() || permission != access(context)) return false;
                        long id = number(rows, "_id"), size = number(rows, "_size"), modified = number(rows, "date_modified");
                        String bucket = text(rows, "bucket_id"), volume = Build.VERSION.SDK_INT >= 29 ? text(rows, "volume_name") : "external";
                        long generation = Build.VERSION.SDK_INT >= 30 ? number(rows, "generation_modified") : 0;
                        int type = (int)number(rows, "media_type");
                        boolean pending = Build.VERSION.SDK_INT >= 29 && number(rows, "is_pending") != 0;
                        boolean trashed = Build.VERSION.SDK_INT >= 30 && number(rows, "is_trashed") != 0;
                        if (PhotosActivity.nativeBackupCandidate(permission, type, selected.contains(bucket), pending, trashed, size, id) != 1) continue;
                        ContentValues value = new ContentValues();
                        value.put("volume", volume); value.put("media_id", id); value.put("bucket", bucket);
                        value.put("name", text(rows, "_display_name")); value.put("mime", text(rows, "mime_type"));
                        value.put("size", size); value.put("modified", modified); value.put("generation", generation);
                        long taken = number(rows, "datetaken");
                        value.put("taken", taken == 0 ? number(rows, "date_added") * 1000 : taken); value.put("width", number(rows, "width"));
                        value.put("height", number(rows, "height")); value.put("media_type", type); value.put("eligible", 1);
                        int retain = 0;
                        value.put("staged_id", ""); value.put("staged_manifest", "");
                        try (Cursor old = db.rawQuery("SELECT confirmed,size,modified,generation,staged_id,staged_manifest FROM items WHERE volume=? AND media_id=?",
                            new String[]{volume, Long.toString(id)})) {
                            if (old.moveToFirst()) {
                                retain = PhotosActivity.nativeRetainBackupConfirmation(old.getInt(0) == 1,
                                    old.getLong(1), size, old.getLong(2), modified, old.getLong(3), generation);
                                if (PhotosActivity.nativeRetainBackupConfirmation(true, old.getLong(1), size, old.getLong(2), modified,
                                    old.getLong(3), generation) == 1) {
                                    value.put("staged_id", old.getString(4)); value.put("staged_manifest", old.getString(5));
                                }
                            }
                        }
                        value.put("confirmed", retain);
                        if (db.insertWithOnConflict("items", null, value, SQLiteDatabase.CONFLICT_REPLACE) < 0) throw new IllegalStateException();
                    }
                    if (canceled.getAsBoolean() || permission != access(context)) return false;
                    db.setTransactionSuccessful();
                } finally { db.endTransaction(); }
                snapshot(db);
                return true;
            } catch (Exception error) {
                queued = 0;
                message = "Device scan paused; queued originals and backup settings were preserved";
                return false;
            }
        }
    }

    private void snapshot(SQLiteDatabase db) {
        Set<String> folders = new HashSet<>();
        try (Cursor rows = db.rawQuery("SELECT bucket FROM folders WHERE selected=1", null)) {
            while (rows.moveToNext()) folders.add(rows.getString(0));
        }
        int ready = 0, waiting = 0, done = 0;
        try (Cursor rows = db.rawQuery("SELECT eligible,confirmed,COUNT(*) FROM items GROUP BY eligible,confirmed", null)) {
            while (rows.moveToNext()) {
                if (rows.getInt(1) == 1) done += rows.getInt(2);
                else if (rows.getInt(0) == 1) ready += rows.getInt(2);
                else waiting += rows.getInt(2);
            }
        }
        int staged = 0;
        try (Cursor rows = db.rawQuery("SELECT COUNT(*) FROM items WHERE eligible=1 AND confirmed=0 AND staged_id<>''", null)) {
            if (rows.moveToFirst()) staged = rows.getInt(0);
        }
        selected = folders; queued = ready; paused = waiting; confirmed = done; prepared = staged;
        message = folders.isEmpty() ? "Choose device folders to queue for ThinkPad backup"
            : ready + " queued; " + waiting + " paused; " + staged + " encrypted, waiting for ThinkPad; " + done + " backed up to ThinkPad.";
    }

    private static final class Original {
        String volume, bucket;
        long id, size, modified, generation;
        int type;
    }

    boolean stageCurrent() {
        Original original = staging;
        if (original == null || closed || prepareCanceled || !selected.contains(original.bucket)) return false;
        int permission = access(context);
        String[] columns = Build.VERSION.SDK_INT >= 30
            ? new String[]{"_size", "date_modified", "generation_modified", "media_type", "is_pending", "is_trashed", "bucket_id"}
            : Build.VERSION.SDK_INT >= 29
            ? new String[]{"_size", "date_modified", "media_type", "is_pending", "bucket_id"}
            : new String[]{"_size", "date_modified", "media_type", "bucket_id"};
        try (Cursor row = context.getContentResolver().query(MediaStore.Files.getContentUri(original.volume), columns,
            "_id=?", new String[]{Long.toString(original.id)}, null)) {
            return row != null && row.moveToFirst() && number(row, "_size") == original.size
                && number(row, "date_modified") == original.modified && text(row, "bucket_id").equals(original.bucket)
                && (Build.VERSION.SDK_INT < 30 || number(row, "generation_modified") == original.generation)
                && PhotosActivity.nativeBackupCandidate(permission, (int)number(row, "media_type"), true,
                    Build.VERSION.SDK_INT >= 29 && number(row, "is_pending") != 0,
                    Build.VERSION.SDK_INT >= 30 && number(row, "is_trashed") != 0, original.size, original.id) == 1;
        } catch (Exception error) { return false; }
    }

    synchronized void prepare(DeviceBackup backup, PhotosActivity activity) {
        if (closed || preparing || !backup.ready) return;
        preparing = true; prepareCanceled = false; prepareFailure = "";
        notifyChanged();
        IO.execute(() -> {
            byte[] identity = null;
            String phase = "identity";
            try {
                identity = backup.read();
                phase = "staging_directory";
                File root = privateDirectory("camera-backup/staged");
                while (!closed && !prepareCanceled) {
                    Original next = new Original();
                    synchronized (STORAGE) {
                        try (SQLiteDatabase db = open(); Cursor row = db.rawQuery(
                            "SELECT volume,media_id,bucket,size,modified,generation,media_type FROM items "
                            + "WHERE eligible=1 AND confirmed=0 AND staged_id='' ORDER BY media_id LIMIT 1", null)) {
                            if (!row.moveToFirst()) break;
                            next.volume = row.getString(0); next.id = row.getLong(1); next.bucket = row.getString(2);
                            next.size = row.getLong(3); next.modified = row.getLong(4); next.generation = row.getLong(5); next.type = row.getInt(6);
                        }
                    }
                    staging = next;
                    phase = "source_permission";
                    if (!stageCurrent()) throw new IllegalStateException();
                    byte[] result;
                    phase = "source_descriptor";
                    try (ParcelFileDescriptor source = context.getContentResolver().openFileDescriptor(
                        ContentUris.withAppendedId(MediaStore.Files.getContentUri(next.volume), next.id), "r")) {
                        if (source == null) throw new IllegalStateException();
                        phase = "native_staging";
                        result = activity.nativeStageBackupOriginal(identity, source.getFd(), next.size, root.getPath().getBytes(StandardCharsets.UTF_8));
                    }
                    if (result == null || !stageCurrent()) throw new IllegalStateException();
                    phase = "manifest";
                    JSONObject manifest = new JSONObject(new String(result, StandardCharsets.UTF_8));
                    String id = manifest.getString("id");
                    if (!id.matches("[a-f0-9]{64}") || manifest.getLong("size") != next.size) throw new IllegalStateException();
                    synchronized (STORAGE) {
                        phase = "queue_publish";
                        try (SQLiteDatabase db = open()) {
                            if (!stageCurrent()) throw new IllegalStateException();
                            ContentValues value = new ContentValues(); value.put("staged_id", id); value.put("staged_manifest", manifest.getJSONObject("manifest").toString());
                            if (db.update("items", value, "volume=? AND media_id=? AND size=? AND modified=? AND generation=? AND eligible=1 AND confirmed=0",
                                new String[]{next.volume, Long.toString(next.id), Long.toString(next.size), Long.toString(next.modified), Long.toString(next.generation)}) != 1) throw new IllegalStateException();
                            snapshot(db);
                        }
                    }
                    staging = null; notifyChanged();
                }
                if (!closed && !prepareCanceled) requestUpload();
            } catch (Exception error) {
                // Debug diagnostics contain only a fixed phase name, never keys,
                // document paths, provider metadata or exception messages.
                if (BuildConfig.DEBUG) prepareFailure = phase;
                message = "Camera preparation paused; originals and queued items were preserved";
            }
            finally {
                if (identity != null) Arrays.fill(identity, (byte)0);
                staging = null; preparing = false; notifyChanged();
            }
        });
    }

    private static final class Pending {
        String volume, name, mime, staged;
        long id, size, taken, width, height;
        boolean video;
    }

    synchronized void requestUpload() {
        if (closed || uploading) return;
        uploading = true; uploadFailure = "";
        notifyChanged();
        NETWORK.execute(() -> {
            try { upload(() -> closed); }
            finally { uploading = false; notifyChanged(); }
        });
    }

    private List<Pending> pending() {
        List<Pending> items = new ArrayList<>();
        synchronized (STORAGE) {
            try (SQLiteDatabase db = open(); Cursor rows = db.rawQuery(
                "SELECT volume,media_id,name,mime,size,taken,width,height,media_type,staged_id FROM items "
                + "WHERE eligible=1 AND confirmed=0 AND staged_id<>'' ORDER BY media_id", null)) {
                while (rows.moveToNext()) {
                    Pending item = new Pending();
                    item.volume = rows.getString(0); item.id = rows.getLong(1); item.name = rows.getString(2); item.mime = rows.getString(3);
                    item.size = rows.getLong(4); item.taken = rows.getLong(5); item.width = rows.getLong(6); item.height = rows.getLong(7);
                    item.video = rows.getInt(8) == 3; item.staged = rows.getString(9);
                    items.add(item);
                }
            }
        }
        return items;
    }

    private long since() {
        synchronized (STORAGE) {
            try (SQLiteDatabase db = open(); Cursor row = db.rawQuery("SELECT value FROM node WHERE key='server_version'", null)) {
                return row.moveToFirst() ? Math.max(0, row.getLong(0)) : 0;
            }
        }
    }

    // Gallery preview in the desktop format; a missing preview never blocks backup.
    private byte[] preview(Pending item) {
        if (Build.VERSION.SDK_INT < 29 || access(context) == 0) return null;
        try {
            Bitmap bitmap = context.getContentResolver().loadThumbnail(
                ContentUris.withAppendedId(MediaStore.Files.getContentUri(item.volume), item.id), new Size(640, 640), null);
            java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
            boolean written = bitmap.compress(Bitmap.CompressFormat.JPEG, 85, output);
            bitmap.recycle();
            return written && output.size() > 0 && output.size() <= 4194304 ? output.toByteArray() : null;
        } catch (Exception error) { return null; }
    }

    // Called by the activity or the OS job on its own worker. Returns true when
    // every prepared item reached the pinned node or nothing was waiting.
    boolean upload(java.util.function.BooleanSupplier canceled) {
        String phase = "queue";
        byte[] identity = null, device = null;
        try {
            List<Pending> items = pending();
            if (items.isEmpty()) return true;
            phase = "identity";
            identity = DeviceBackup.readIdentity(context);
            device = DeviceBackup.device(context);
            File root = privateDirectory("camera-backup/staged");
            phase = "node";
            try (PinnedNode node = new PinnedNode(identity, device)) {
                node.open();
                long since = since();
                for (Pending item : items) {
                    if (canceled.getAsBoolean() || closed) return false;
                    if (!item.staged.matches("[0-9a-f]{64}")) continue;
                    phase = "manifest";
                    File folder = new File(root, item.staged);
                    byte[] staged;
                    try (java.io.InputStream input = new java.io.FileInputStream(new File(folder, "manifest.json"))) {
                        java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
                        byte[] buffer = new byte[65536]; int count;
                        while ((count = input.read(buffer)) > 0) {
                            if (output.size() + count > 2097152) throw new IllegalStateException();
                            output.write(buffer, 0, count);
                        }
                        staged = output.toByteArray();
                    }
                    JSONObject manifest = new JSONObject(new String(staged, StandardCharsets.UTF_8));
                    if (!manifest.getString("id").equals(item.staged) || manifest.getLong("size") != item.size) throw new IllegalStateException();
                    phase = "chunks";
                    JSONArray chunks = manifest.getJSONObject("manifest").getJSONArray("chunks");
                    for (int index = 0; index < chunks.length(); index++) {
                        if (canceled.getAsBoolean() || closed) return false;
                        File chunk = new File(folder, index + ".blob");
                        if (!chunk.isFile() || !node.blob(chunks.getString(index), null, chunk)) throw new IllegalStateException();
                    }
                    phase = "preview";
                    String previewId = "", previewBlob = "";
                    byte[] jpeg = preview(item);
                    if (jpeg != null) {
                        byte[] sealed = PhotosActivity.nativeBackupPreview(identity, jpeg);
                        if (sealed != null && sealed.length > 128) {
                            String id = new String(sealed, 0, 64, StandardCharsets.US_ASCII), blob = new String(sealed, 64, 64, StandardCharsets.US_ASCII);
                            if (node.blob(blob, Arrays.copyOfRange(sealed, 128, sealed.length), null)) { previewId = id; previewBlob = blob; }
                        }
                    }
                    phase = "record";
                    long version = -1;
                    for (int attempt = 0; attempt < 2 && version < 0; attempt++) {
                        byte[] request = PhotosActivity.nativeBackupRequest(identity, node.clientBytes(), since,
                            item.name.getBytes(StandardCharsets.UTF_8), item.mime.getBytes(StandardCharsets.UTF_8), item.video,
                            item.taken, item.width, item.height, staged, previewId.getBytes(StandardCharsets.US_ASCII),
                            previewBlob.getBytes(StandardCharsets.US_ASCII));
                        if (request == null) throw new IllegalStateException();
                        PinnedNode.Reply reply = node.signed("POST", "/api/v1/sync", request, null, PinnedNode.digest(request), 8388608);
                        // A rebuilt node restarts its counter; resend from zero once.
                        if (reply.status == 409 && since > 0) { since = 0; continue; }
                        if (reply.status != 200) throw new IllegalStateException();
                        version = PhotosActivity.nativeBackupConfirmed(reply.body, since);
                    }
                    if (version < 0) throw new IllegalStateException();
                    since = version;
                    phase = "confirm";
                    synchronized (STORAGE) {
                        try (SQLiteDatabase db = open()) {
                            db.beginTransaction();
                            try {
                                ContentValues cursor = new ContentValues(); cursor.put("key", "server_version"); cursor.put("value", version);
                                db.insertWithOnConflict("node", null, cursor, SQLiteDatabase.CONFLICT_REPLACE);
                                db.execSQL("UPDATE items SET confirmed=1 WHERE volume=? AND media_id=? AND staged_id=? AND confirmed=0",
                                    new Object[]{item.volume, item.id, item.staged});
                                db.setTransactionSuccessful();
                            } finally { db.endTransaction(); }
                            // The ThinkPad holds this ciphertext now; free the local copy
                            // once no other queued version still needs it.
                            boolean needed;
                            try (Cursor row = db.rawQuery("SELECT COUNT(*) FROM items WHERE staged_id=? AND confirmed=0", new String[]{item.staged})) {
                                needed = !row.moveToFirst() || row.getInt(0) > 0;
                            }
                            if (!needed) {
                                File[] files = folder.listFiles();
                                if (files != null) for (File file : files) file.delete();
                                folder.delete();
                            }
                            snapshot(db);
                        }
                    }
                    notifyChanged();
                }
            }
            return true;
        } catch (Exception error) {
            // Diagnostics name only a fixed phase, never keys, paths or replies.
            if (BuildConfig.DEBUG) uploadFailure = error instanceof PinnedNode.Refused ? ((PinnedNode.Refused)error).phase : phase;
            message = "ThinkPad upload paused; encrypted copies and originals were preserved";
            return false;
        } finally {
            if (identity != null) Arrays.fill(identity, (byte)0);
            if (device != null) Arrays.fill(device, (byte)0);
        }
    }

    private static String text(Cursor row, String name) { int at = row.getColumnIndexOrThrow(name); return row.isNull(at) ? "" : row.getString(at); }
    private static long number(Cursor row, String name) { int at = row.getColumnIndexOrThrow(name); return row.isNull(at) ? 0 : row.getLong(at); }
    private JobScheduler scheduler() { return (JobScheduler)context.getSystemService(Context.JOB_SCHEDULER_SERVICE); }
    private void schedule() {
        JobInfo old = scheduler().getPendingJob(PERIODIC_JOB);
        if (old == null) scheduler().schedule(new JobInfo.Builder(PERIODIC_JOB,
            new ComponentName(context, BackupScanJob.class)).setPeriodic(15 * 60 * 1000L).setPersisted(true).build());
    }
    private void notifyChanged() { if (!closed) changed.run(); }
    boolean folderSelected(String bucket) { return selected.contains(bucket); }
    int folderCount() { return selected.size(); }
    void close() { closed = true; prepareCanceled = true; }
}
