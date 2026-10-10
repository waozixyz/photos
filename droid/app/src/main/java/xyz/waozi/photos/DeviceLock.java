package xyz.waozi.photos;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Build;
import android.os.CancellationSignal;
import android.util.Log;
import android.view.WindowManager;

/** Device authentication and private OS preferences; Photos renders its UI in Zi. */
final class DeviceLock {
    static final int REQUEST = 330;
    private final Activity activity;
    private final Runnable changed;
    private final SharedPreferences preferences;
    private final KeyguardManager keyguard;
    private CancellationSignal cancellation;
    private int revision;
    private String purpose = "";
    private boolean credential;
    private boolean closed;
    volatile boolean enabled;
    volatile boolean locked;
    volatile boolean hidden;
    volatile boolean pending;
    volatile String message = "";

    DeviceLock(Activity activity, Runnable changed) {
        this.activity = activity; this.changed = changed;
        preferences = activity.getSharedPreferences("security", Activity.MODE_PRIVATE);
        keyguard = activity.getSystemService(KeyguardManager.class);
        enabled = preferences.getBoolean("device_lock", false);
        hidden = preferences.getBoolean("hide_content", false);
        locked = enabled;
        applyPrivacy();
    }

    boolean secure() { return keyguard != null && keyguard.isDeviceSecure(); }
    boolean allowed() { return !closed && (!enabled || !locked); }

    private void applyPrivacy() {
        if (hidden) activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        else activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
    }

    void pause() {
        if (BuildConfig.DEBUG) Log.d("PhotosLock", "pause pending=" + pending + " credential=" + credential + " purpose=" + purpose);
        if (enabled) locked = true;
        // The credential activity owns its result. A biometric prompt is
        // canceled if Photos itself goes into the background.
        if (pending && !credential) cancel();
    }

    private void cancel() {
        revision++; pending = false; purpose = "";
        if (cancellation != null) cancellation.cancel();
        cancellation = null;
    }

    void close() { closed = true; cancel(); locked = enabled; }

    void action(String action) {
        if (closed || activity.isFinishing() || activity.isDestroyed()) return;
        if (action.equals("unlock")) { if (enabled && locked) authenticate("unlock"); }
        else if (action.equals("enable_lock")) { if (!enabled) authenticate("enable"); }
        else if (action.equals("disable_lock")) { if (enabled && !locked) authenticate("disable"); }
        else if (action.equals("lock")) { if (enabled) { cancel(); locked = true; changed.run(); } }
        else if (action.equals("hide_content") && allowed()) {
            if (preferences.edit().putBoolean("hide_content", !hidden).commit()) { hidden = !hidden; applyPrivacy(); }
            else message = "Could not save content privacy";
            changed.run();
        } else if (action.equals("device_security")) {
            activity.startActivity(new Intent(android.provider.Settings.ACTION_SECURITY_SETTINGS));
        }
    }

    private void authenticate(String next) {
        if (pending) return;
        if (!secure()) { message = "Set a device screen lock first"; changed.run(); return; }
        pending = true; purpose = next; credential = false; message = "";
        final int request = ++revision;
        if (BuildConfig.DEBUG) Log.d("PhotosLock", "authenticate " + next + " revision=" + request);
        changed.run();
        cancellation = new CancellationSignal();
        BiometricPrompt.Builder prompt = new BiometricPrompt.Builder(activity)
            .setTitle(next.equals("enable") ? "Protect Photos" : next.equals("disable") ? "Turn off Photos lock" : "Unlock Photos");
        if (Build.VERSION.SDK_INT >= 30) {
            // Let Android own the biometric/credential switch and its result.
            // Opening a second credential activity after a biometric failure
            // can overlap the canceled prompt's dismissal and lose the grant.
            prompt.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG |
                BiometricManager.Authenticators.DEVICE_CREDENTIAL);
        } else {
            prompt.setNegativeButton("Use device screen lock", activity.getMainExecutor(), (dialog, which) -> credential(request));
        }
        try {
            prompt.build().authenticate(cancellation, activity.getMainExecutor(), new BiometricPrompt.AuthenticationCallback() {
                @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) { finish(request, true); }
                @Override public void onAuthenticationError(int code, CharSequence ignored) {
                    if (request != revision || !pending || credential || closed) return;
                    if (Build.VERSION.SDK_INT < 30 && (code == BiometricPrompt.BIOMETRIC_ERROR_NO_BIOMETRICS || code == BiometricPrompt.BIOMETRIC_ERROR_HW_NOT_PRESENT ||
                        code == BiometricPrompt.BIOMETRIC_ERROR_HW_UNAVAILABLE || code == BiometricPrompt.BIOMETRIC_ERROR_LOCKOUT ||
                        code == BiometricPrompt.BIOMETRIC_ERROR_LOCKOUT_PERMANENT)) credential(request);
                    else finish(request, false);
                }
            });
        } catch (RuntimeException error) { credential(request); }
    }

    private void credential(int request) {
        if (closed || request != revision || !pending || credential) return;
        Intent intent = keyguard.createConfirmDeviceCredentialIntent("Photos", "Confirm your device screen lock");
        if (intent == null) { finish(request, false); return; }
        credential = true;
        try { activity.startActivityForResult(intent, REQUEST); }
        catch (RuntimeException error) { credential = false; finish(request, false); }
    }

    void result(int resultCode) {
        if (pending && credential && !closed) finish(revision, resultCode == Activity.RESULT_OK);
    }

    private void finish(int request, boolean success) {
        if (BuildConfig.DEBUG) Log.d("PhotosLock", "finish revision=" + request + " current=" + revision + " success=" + success + " pending=" + pending);
        if (closed || request != revision || !pending) return;
        String next = purpose; pending = false; credential = false; purpose = ""; cancellation = null;
        if (success && secure()) {
            boolean value = !next.equals("disable");
            if (next.equals("unlock") || preferences.edit().putBoolean("device_lock", value).commit()) {
                enabled = value; locked = false; message = "";
            } else { locked = enabled; message = "Could not save app lock"; }
        } else { locked = enabled; message = "Authentication canceled"; }
        changed.run();
    }
}
