package xyz.waozi.photos;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.BooleanSupplier;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Android document access and OS key storage. Recovery validation is Ziran. */
final class DeviceBackup {
    static final int REQUEST = 331;
    private static final String KEY = "xyz.waozi.photos.backup.identity";
    private static final byte[] CONTEXT = "photos-android-identity-v1".getBytes(StandardCharsets.UTF_8);
    private static final String DEVICE_KEY = "xyz.waozi.photos.backup.device";
    private static final byte[] DEVICE_CONTEXT = "photos-android-device-v1".getBytes(StandardCharsets.UTF_8);
    private static final Object DEVICE = new Object();
    private final PhotosActivity activity;
    private final BooleanSupplier allowed;
    private final Runnable changed;
    private final AtomicFile file;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private Uri selected;
    private volatile boolean closed;
    volatile boolean configured;
    volatile boolean ready;
    volatile boolean pending;
    volatile boolean recoverySelected;
    volatile String message = "Camera backup setup is in progress";

    DeviceBackup(PhotosActivity activity, BooleanSupplier allowed, Runnable changed) {
        this.activity = activity; this.allowed = allowed; this.changed = changed;
        file = new AtomicFile(new File(activity.getFilesDir(), "backup-identity"));
        configured = file.getBaseFile().exists() || new File(file.getBaseFile() + ".bak").exists();
        if (configured) {
            byte[] identity = null;
            try { identity = read(); ready = true; message = "Backup keys ready; photos back up only to your pinned ThinkPad"; }
            catch (Exception error) { message = "Existing backup keys are unavailable; they were preserved"; }
            finally { if (identity != null) Arrays.fill(identity, (byte)0); }
        }
    }

    void choose() {
        if (closed || pending || configured || !allowed.getAsBoolean()) return;
        selected = null; recoverySelected = false;
        activity.startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*")
            .addCategory(Intent.CATEGORY_OPENABLE), REQUEST);
    }

    void result(int result, Intent data) {
        if (closed || configured || pending) return;
        selected = result == Activity.RESULT_OK && data != null ? data.getData() : null;
        recoverySelected = selected != null;
        message = recoverySelected ? "Enter this recovery file's separate password" : "Recovery file selection canceled";
        changed.run();
    }

    synchronized void restore(byte[] input) {
        byte[] password = input.clone(); Arrays.fill(input, (byte)0);
        if (closed || pending || configured || selected == null || !allowed.getAsBoolean()) {
            Arrays.fill(password, (byte)0); return;
        }
        final Uri source = selected;
        pending = true; message = "Checking recovery file"; changed.run();
        executor.execute(() -> {
            byte[] recovery = null, identity = null, cipher = null;
            try {
                try (InputStream in = activity.getContentResolver().openInputStream(source)) {
                    if (in == null) throw new IllegalArgumentException();
                    recovery = bytes(in, 65609);
                }
                if (recovery.length > 65608) throw new IllegalArgumentException();
                identity = activity.nativeReadBackupIdentity(recovery, password);
                if (identity == null || identity.length != 3968) {
                    message = "Incorrect recovery password or invalid recovery file";
                    return;
                }
                // Never replace an account, or publish after private access ends.
                if (closed || !allowed.getAsBoolean() || file.getBaseFile().exists()) {
                    message = "Unlock Photos and retry; existing backup keys were preserved"; return;
                }
                Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
                aes.init(Cipher.ENCRYPT_MODE, key(KEY)); aes.updateAAD(CONTEXT);
                cipher = aes.doFinal(identity);
                FileOutputStream out = null;
                try {
                    out = file.startWrite();
                    out.write(aes.getIV()); out.write(cipher);
                    if (closed || !allowed.getAsBoolean()) throw new IllegalStateException();
                    file.finishWrite(out); out = null;
                    configured = true; ready = true; recoverySelected = false; selected = null;
                    message = "Backup keys ready; photos back up only to your pinned ThinkPad";
                } finally { if (out != null) file.failWrite(out); }
            } catch (Exception error) { message = "Could not restore backup keys; existing keys were preserved"; }
            finally {
                Arrays.fill(password, (byte)0);
                if (recovery != null) Arrays.fill(recovery, (byte)0);
                if (identity != null) Arrays.fill(identity, (byte)0);
                if (cipher != null) Arrays.fill(cipher, (byte)0);
                pending = false;
                activity.runOnUiThread(() -> { if (!closed) changed.run(); });
            }
        });
    }

    private static SecretKey key(String alias) throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (store.containsAlias(alias)) return (SecretKey)store.getKey(alias, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256).setRandomizedEncryptionRequired(true).build());
        return generator.generateKey();
    }

    byte[] read() throws Exception { return readIdentity(activity); }

    static boolean configured(Context context) {
        File base = new File(context.getFilesDir(), "backup-identity");
        return base.exists() || new File(base + ".bak").exists();
    }

    /** The validated identity, unwrapped for one operation; callers zero it. */
    static byte[] readIdentity(Context context) throws Exception {
        return open(new AtomicFile(new File(context.getFilesDir(), "backup-identity")), KEY, CONTEXT, 3968);
    }

    /** This phone's Daochi device seed (32 bytes) and client id, created once
     * by Ziran and sealed by the OS keystore. The account identity is unchanged. */
    static byte[] device(Context context) throws Exception {
        synchronized (DEVICE) {
            AtomicFile file = new AtomicFile(new File(context.getFilesDir(), "backup-device"));
            if (file.getBaseFile().exists() || new File(file.getBaseFile() + ".bak").exists()) return open(file, DEVICE_KEY, DEVICE_CONTEXT, 71);
            byte[] fresh = PhotosActivity.nativeBackupNewDevice(), cipher = null;
            if (fresh == null || fresh.length != 71) throw new IllegalStateException();
            FileOutputStream out = null;
            try {
                Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
                aes.init(Cipher.ENCRYPT_MODE, key(DEVICE_KEY)); aes.updateAAD(DEVICE_CONTEXT);
                cipher = aes.doFinal(fresh);
                out = file.startWrite();
                out.write(aes.getIV()); out.write(cipher);
                file.finishWrite(out); out = null;
                return fresh.clone();
            } finally {
                if (out != null) file.failWrite(out);
                Arrays.fill(fresh, (byte)0);
                if (cipher != null) Arrays.fill(cipher, (byte)0);
            }
        }
    }

    private static byte[] open(AtomicFile file, String alias, byte[] context, int size) throws Exception {
        byte[] stored;
        try (InputStream input = file.openRead()) { stored = bytes(input, size + 29); }
        try {
            if (stored.length != size + 28) throw new IllegalArgumentException();
            KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
            if (!store.containsAlias(alias)) throw new IllegalStateException();
            Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
            aes.init(Cipher.DECRYPT_MODE, store.getKey(alias, null), new GCMParameterSpec(128, stored, 0, 12));
            aes.updateAAD(context);
            byte[] value = aes.doFinal(stored, 12, stored.length - 12);
            if (value.length != size) { Arrays.fill(value, (byte)0); throw new IllegalArgumentException(); }
            return value;
        } finally { Arrays.fill(stored, (byte)0); }
    }

    void close() { closed = true; selected = null; executor.shutdown(); }

    private static byte[] bytes(InputStream input, int limit) throws Exception {
        byte[] buffer = new byte[limit];
        try {
            int count = 0, length;
            while (count < limit && (length = input.read(buffer, count, limit - count)) >= 0) {
                if (length == 0) throw new IllegalStateException();
                count += length;
            }
            return Arrays.copyOf(buffer, count);
        } finally { Arrays.fill(buffer, (byte)0); }
    }
}
