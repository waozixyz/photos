package xyz.waozi.photos;

import android.app.job.JobParameters;
import android.app.job.JobService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** Periodic MediaStore discovery, then upload of already encrypted originals
 * to the pinned ThinkPad node over USB loopback. Staging happens in the app. */
public final class BackupScanJob extends JobService {
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancellation;

    @Override public boolean onStartJob(JobParameters parameters) {
        AtomicBoolean stopped = new AtomicBoolean(); cancellation = stopped;
        executor.execute(() -> {
            BackupQueue queue = new BackupQueue(this, () -> {}, false);
            boolean success;
            try {
                success = queue.scan(stopped::get);
                if (success && !stopped.get() && DeviceBackup.configured(this)) success = queue.upload(stopped::get);
            }
            finally { queue.close(); }
            if (!stopped.get()) jobFinished(parameters, !success);
        });
        return true;
    }

    @Override public boolean onStopJob(JobParameters parameters) {
        AtomicBoolean stopped = cancellation;
        if (stopped != null) stopped.set(true);
        return true;
    }

    @Override public void onDestroy() {
        AtomicBoolean stopped = cancellation;
        if (stopped != null) stopped.set(true);
        executor.shutdown(); super.onDestroy();
    }
}
