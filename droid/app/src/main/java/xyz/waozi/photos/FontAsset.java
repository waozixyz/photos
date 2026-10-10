package xyz.waozi.photos;

import android.content.Context;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.security.MessageDigest;

/** Copy the licensed bundled TrueType UI font for the native host. */
final class FontAsset {
    private static final String DIGEST = "b836f4c3a5bd7d928893fb909eb2f02902143b12fc166337fc4c72e3bdb2d7f3";
    static void prepare(Context context) {
        AtomicFile file = new AtomicFile(new File(context.getFilesDir(), "ui-typeface.ttf"));
        try {
            if (file.getBaseFile().isFile()) {
                try (InputStream in = file.openRead()) { if (digest(in).equals(DIGEST)) return; }
                catch (Exception error) { /* Replace an unreadable cache from the verified bundled asset. */ }
            }
            FileOutputStream output = null;
            try (InputStream input = context.getAssets().open("fonts/NotoSansJP-Regular.ttf")) {
                output = file.startWrite();
                MessageDigest hash = MessageDigest.getInstance("SHA-256");
                byte[] bytes = new byte[65536]; int count; long total = 0;
                while ((count = input.read(bytes)) > 0) {
                    total += count;
                    if (total > 16 * 1024 * 1024) throw new IllegalArgumentException();
                    hash.update(bytes, 0, count); output.write(bytes, 0, count);
                }
                if (!hex(hash.digest()).equals(DIGEST)) throw new IllegalArgumentException();
                file.finishWrite(output); output = null;
            } finally { if (output != null) file.failWrite(output); }
        } catch (Exception error) {
            // Keep the prior valid font. The host can use system fallback.
        }
    }
    private static String digest(InputStream input) throws Exception {
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        byte[] bytes = new byte[65536]; int count; long total = 0;
        while ((count = input.read(bytes)) > 0) {
            total += count;
            if (total > 16 * 1024 * 1024) throw new IllegalArgumentException();
            hash.update(bytes, 0, count);
        }
        return hex(hash.digest());
    }
    private static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray(), value = new char[bytes.length * 2];
        for (int i = 0; i < bytes.length; i++) { value[i*2] = alphabet[(bytes[i] & 255) >>> 4]; value[i*2+1] = alphabet[bytes[i] & 15]; }
        return new String(value);
    }
}
