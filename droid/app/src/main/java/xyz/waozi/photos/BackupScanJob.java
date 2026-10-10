package xyz.waozi.photos;

import android.app.job.JobParameters;
import android.app.job.JobService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** Periodic MediaStore discovery, encryption of new eligible originals and
 * upload to the pinned ThinkPad node over USB loopback, with Photos closed. */
public final class BackupScanJob extends JobService {
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancellation;
    private volatile BackupQueue running;

    @Override public boolean onStartJob(JobParameters parameters) {
        AtomicBoolean stopped = new AtomicBoolean(); cancellation = stopped;
        executor.execute(() -> {
            BackupQueue queue = new BackupQueue(this, () -> {}, false);
            boolean success;
            running = queue;
            try {
                success = queue.scan(stopped::get);
                if (success && !stopped.get() && DeviceBackup.configured(this)) {
                    // New camera photos are encrypted here even while Photos is
                    // closed; the queue guards permission and version per chunk.
                    // While Photos is on screen its own Prepare path, which
                    // honors the app lock, handles staging instead.
                    boolean staged = PhotosActivity.visible || queue.stageAll(queue, stopped::get);
                    boolean uploaded = !stopped.get() && queue.upload(stopped::get);
                    success = staged && uploaded;
                }
            }
            finally { queue.close(); running = null; }
            if (!stopped.get()) jobFinished(parameters, !success);
        });
        return true;
    }

    @Override public boolean onStopJob(JobParameters parameters) {
        AtomicBoolean stopped = cancellation;
        if (stopped != null) stopped.set(true);
        BackupQueue queue = running;
        if (queue != null) queue.close();
        return true;
    }

    @Override public void onDestroy() {
        AtomicBoolean stopped = cancellation;
        if (stopped != null) stopped.set(true);
        executor.shutdown(); super.onDestroy();
    }
}
