package xyz.waozi.photos;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import org.json.JSONObject;

/** Loopback-only HTTP to the owner's ThinkPad Daochi node. The ThinkPad
 * forwards this phone's 127.0.0.1 port over USB; there is no other route.
 * Ziran pins the node identity, signs every request and seals every record. */
final class PinnedNode implements AutoCloseable {
    static final int PORT = 18080;
    private static final String ORIGIN = "http://127.0.0.1:" + PORT;
    private static final int GET = 1, POST = 2, PUT = 3, HEAD = 4;
    private static final byte[] EMPTY_DIGEST = digest(new byte[0]);

    static final class Reply {
        final int status; final byte[] body;
        Reply(int status, byte[] body) { this.status = status; this.body = body; }
    }

    static final class Refused extends Exception {
        final String phase;
        Refused(String phase) { this.phase = phase; }
    }

    private final byte[] identity, seed, client;
    private final String account;
    private String token;

    PinnedNode(byte[] identity, byte[] device) {
        this.identity = identity.clone();
        seed = Arrays.copyOf(device, 32);
        client = Arrays.copyOfRange(device, 32, device.length);
        account = hex(Arrays.copyOfRange(identity, 32, 64));
    }

    @Override public void close() { Arrays.fill(identity, (byte)0); Arrays.fill(seed, (byte)0); token = null; }

    /** Check the pinned identity and replication policy, then log in and register this device. */
    void open() throws Exception {
        Reply node = send("GET", "/api/v1/node", null, null, null, 1 << 20);
        if (node.status != 200 || PhotosActivity.nativeBackupNode(identity, node.body) != 0) throw new Refused("node");
        Reply challenge = send("GET", "/api/v1/sync/challenge?user_id=" + account, null, null, null, 1 << 16);
        if (challenge.status != 200) throw new Refused("challenge");
        byte[] login = PhotosActivity.nativeBackupLogin(identity, client, challenge.body);
        if (login == null || login.length < 4) throw new Refused("login");
        int count = ((login[0] & 255) << 24) | ((login[1] & 255) << 16) | ((login[2] & 255) << 8) | (login[3] & 255);
        if (count < 1 || count > login.length - 4) throw new Refused("login");
        byte[] body = Arrays.copyOfRange(login, 4, 4 + count);
        String signature = new String(login, 4 + count, login.length - 4 - count, StandardCharsets.US_ASCII);
        Reply session = send("POST", "/api/v1/sync/login", body, null,
            new String[]{"X-Daochi-User", account, "X-Daochi-Signature", signature}, 1 << 16);
        if (session.status != 200) throw new Refused("login");
        token = new JSONObject(new String(session.body, StandardCharsets.UTF_8)).getString("auth_token");
        if (!token.matches("[\\x21-\\x7e]{1,4096}")) throw new Refused("login");
        byte[] registration = PhotosActivity.nativeBackupRegistration(identity, seed, client, System.currentTimeMillis() / 1000);
        if (registration == null) throw new Refused("device");
        Reply device = send("POST", "/api/v1/account/devices", registration, null, new String[]{"Authorization", "Bearer " + token}, 1 << 16);
        if (device.status < 200 || device.status >= 300) throw new Refused("device");
    }

    /** Bearer and X-Daochi-Tx signed request; the digest covers the exact body. */
    Reply signed(String method, String path, byte[] body, File file, byte[] digest, int limit) throws Exception {
        int code = method.equals("GET") ? GET : method.equals("POST") ? POST : method.equals("PUT") ? PUT : HEAD;
        byte[] header = PhotosActivity.nativeBackupHeader(identity, seed, code, path.getBytes(StandardCharsets.US_ASCII),
            digest, System.currentTimeMillis() / 1000 + 120);
        if (header == null || token == null) throw new Refused("signature");
        return send(method, path, body, file, new String[]{"Authorization", "Bearer " + token, "X-Daochi-User", account,
            "X-Daochi-Tx", new String(header, StandardCharsets.US_ASCII)}, limit);
    }

    /** Content-addressed encrypted blob; true once the node holds it. */
    boolean blob(String digest, byte[] data, File file) throws Exception {
        if (!digest.matches("[0-9a-f]{64}")) return false;
        String path = "/api/v1/blobs/photos/" + digest;
        Reply head = signed("HEAD", path, null, null, EMPTY_DIGEST, 0);
        if (head.status == 200) return true;
        if (head.status != 404) return false;
        Reply put = signed("PUT", path, data, file, unhex(digest), 1 << 16);
        if (put.status < 200 || put.status >= 300) return false;
        return signed("HEAD", path, null, null, EMPTY_DIGEST, 0).status == 200;
    }

    String client() { return new String(client, StandardCharsets.US_ASCII); }
    byte[] clientBytes() { return client.clone(); }

    private static Reply send(String method, String path, byte[] body, File file, String[] headers, int limit) throws Exception {
        HttpURLConnection connection = (HttpURLConnection)new URL(ORIGIN + path).openConnection(Proxy.NO_PROXY);
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(60000);
            connection.setRequestMethod(method);
            if (headers != null) for (int i = 0; i < headers.length; i += 2) connection.setRequestProperty(headers[i], headers[i + 1]);
            if (body != null || file != null) {
                long length = file != null ? file.length() : body.length;
                connection.setRequestProperty("Content-Type", method.equals("PUT") ? "application/octet-stream" : "application/json");
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(length);
                try (OutputStream output = connection.getOutputStream()) {
                    if (file != null) {
                        try (InputStream input = new FileInputStream(file)) {
                            byte[] buffer = new byte[65536]; int count; long sent = 0;
                            while ((count = input.read(buffer)) > 0) { output.write(buffer, 0, count); sent += count; }
                            if (sent != length) throw new IllegalStateException();
                        }
                    } else output.write(body);
                }
            }
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) throw new Refused("redirect");
            if (method.equals("HEAD")) return new Reply(status, new byte[0]);
            InputStream input = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            if (input != null) {
                try (InputStream stream = input) {
                    byte[] buffer = new byte[16384]; int count;
                    while ((count = stream.read(buffer)) > 0) {
                        if (output.size() + count > limit) throw new Refused("response_size");
                        output.write(buffer, 0, count);
                    }
                }
            }
            return new Reply(status, output.toByteArray());
        } finally { connection.disconnect(); }
    }

    static byte[] digest(byte[] bytes) {
        try { return MessageDigest.getInstance("SHA-256").digest(bytes); }
        catch (java.security.NoSuchAlgorithmException error) { throw new IllegalStateException(error); }
    }

    static String hex(byte[] bytes) {
        StringBuilder text = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) text.append(Character.forDigit((value >> 4) & 15, 16)).append(Character.forDigit(value & 15, 16));
        return text.toString();
    }

    private static byte[] unhex(String text) {
        byte[] bytes = new byte[text.length() / 2];
        for (int i = 0; i < bytes.length; i++) bytes[i] = (byte)Integer.parseInt(text.substring(i * 2, i * 2 + 2), 16);
        return bytes;
    }
}
