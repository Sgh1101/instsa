package app.instsave;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.IBinder;
import android.os.PowerManager;

import java.util.Map;
import java.util.Random;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 저장을 앱 화면과 별개로 돌리는 포그라운드 서비스. 알림에 "저장 중…"이 떠 있는 동안에는
 * 사용자가 앱을 나가거나 화면을 꺼도 안드로이드가 프로세스를 끊지 않습니다.
 *
 * 요청은 한 번에 하나씩 순서대로 처리합니다. 여러 링크를 한꺼번에 저장하는 "묶음"은
 *  - 게시물 사이에 3~6초 간격을 두고(인스타그램 요청 제한을 덜 건드리려고),
 *  - 요청 제한(429)이나 로그인 필요(401)가 나오면 남은 항목을 중단하고(계속하면 계정만 더 막힘),
 *  - 이미 받은 게시물은 인스타그램에 요청하지 않고 건너뛰며,
 *  - 끝나면 알림 한 번으로 요약합니다.
 */
public class SaveService extends Service {
    /** 화면이 떠 있을 때 결과를 화면(JS)으로 돌려주는 통로. 화면이 없으면 null. */
    interface Listener {
        void onResult(int id, boolean ok, String message);

        void onBatch(int batch, int done, int total, int saved, int skipped, int failed, boolean stopped,
                     boolean finished, String message);
    }

    static volatile Listener listener;
    /** 앱이 화면에 보이는 중인가. 보이면 결과를 화면에 알리므로 완료 알림은 생략합니다. */
    static volatile boolean appVisible;

    /** 묶음 하나의 진행 상황. */
    private static final class Batch {
        final int total;
        int done;
        int saved;
        int skipped;
        int failed;
        boolean stopped;        // 요청 제한/로그인 필요로 중단됨
        boolean lastWasNetwork; // 직전 항목이 인스타그램에 요청했는가(했으면 다음 항목 전에 간격을 둠)
        Uri lastUri;
        String lastMime;

        Batch(int total) {
            this.total = total;
        }
    }

    private static final Map<Integer, Batch> BATCHES = new ConcurrentHashMap<>();
    private static final AtomicInteger NEXT_BATCH = new AtomicInteger(1);

    private static final String CH_PROGRESS = "saving";
    private static final String CH_DONE = "done";
    private static final int FGS_ID = 1;
    private static final long WAKE_LOCK_MS = 10 * 60 * 1000L;
    private static final int GAP_MIN_MS = 3000;
    private static final int GAP_JITTER_MS = 3000;

    private final AtomicInteger pending = new AtomicInteger();
    private final AtomicInteger sessionTotal = new AtomicInteger();
    private final AtomicInteger sessionDone = new AtomicInteger();
    private final Random random = new Random();
    private ExecutorService worker;
    private NotificationManager nm;

    /** 낱개 저장. 앱이 화면에 있을 때 불러야 합니다(안드로이드 12+는 백그라운드에서 포그라운드 서비스를 시작할 수 없음). */
    static void enqueue(Context context, int id, String url, int index) {
        send(context, id, url, index, 0, false);
    }

    /** 묶음 저장을 등록하고 번호를 돌려줍니다. 이어서 항목마다 enqueueInBatch를 부르세요. */
    static int registerBatch(int total) {
        int batch = NEXT_BATCH.getAndIncrement();
        BATCHES.put(batch, new Batch(total));
        return batch;
    }

    static void enqueueInBatch(Context context, int batch, String url, boolean skipSaved) {
        send(context, 0, url, 0, batch, skipSaved);
    }

    private static void send(Context context, int id, String url, int index, int batch, boolean skipSaved) {
        Intent i = new Intent(context, SaveService.class)
                .putExtra("id", id)
                .putExtra("url", url)
                .putExtra("index", index)
                .putExtra("batch", batch)
                .putExtra("skipSaved", skipSaved);
        context.startForegroundService(i);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        worker = Executors.newSingleThreadExecutor();
        nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH_PROGRESS, "저장 중", NotificationManager.IMPORTANCE_LOW));
        nm.createNotificationChannel(new NotificationChannel(CH_DONE, "저장 완료", NotificationManager.IMPORTANCE_DEFAULT));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            // 시스템이 서비스를 다시 만든 경우: 할 일이 없으니 포그라운드 약속(5초 안에 startForeground)만 지키고 끝냅니다.
            startForeground(FGS_ID, progressNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }
        final int id = intent.getIntExtra("id", 0);
        final String url = intent.getStringExtra("url");
        final int index = intent.getIntExtra("index", 0);
        final int batch = intent.getIntExtra("batch", 0);
        final boolean skipSaved = intent.getBooleanExtra("skipSaved", false);

        pending.incrementAndGet();
        sessionTotal.incrementAndGet();
        // 서비스를 시작한 뒤 5초 안에 불러야 합니다. 안드로이드 14+는 유형(dataSync) 지정이 필수입니다.
        startForeground(FGS_ID, progressNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        worker.submit(() -> run(id, url, index, batch, skipSaved));
        return START_NOT_STICKY;
    }

    private void run(int id, String url, int index, int batch, boolean skipSaved) {
        PowerManager.WakeLock wakeLock = getSystemService(PowerManager.class)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "instasave:save");
        wakeLock.acquire(WAKE_LOCK_MS); // 화면이 꺼져도 CPU가 잠들지 않게(시간 제한 있음)
        Saver.Result r;
        try {
            r = execute(url, index, batch, skipSaved);
        } catch (Throwable t) {
            r = Saver.Result.failed("저장하지 못했어요: " + t.getMessage(), 0);
        } finally {
            if (wakeLock.isHeld()) {
                wakeLock.release();
            }
        }

        if (batch != 0) {
            finishBatchItem(batch, r);
        } else {
            Listener l = listener;
            if (l != null) {
                l.onResult(id, r.ok, r.message);
            }
            if (!appVisible) {
                notifyDone(100 + id, r.ok ? "저장 완료" : "저장 실패", r.message, r.ok, r.lastUri, r.lastMime);
            }
        }

        sessionDone.incrementAndGet();
        if (pending.decrementAndGet() == 0) {
            stopForeground(STOP_FOREGROUND_REMOVE);
            sessionTotal.set(0);
            sessionDone.set(0);
            stopSelf();
        } else {
            nm.notify(FGS_ID, progressNotification());
        }
    }

    private Saver.Result execute(String url, int index, int batch, boolean skipSaved) throws InterruptedException {
        Batch b = batch != 0 ? BATCHES.get(batch) : null;
        if (b == null) {
            return Saver.save(this, url, index, false);
        }
        boolean stopped;
        boolean needGap;
        synchronized (b) {
            stopped = b.stopped;
            needGap = b.lastWasNetwork;
        }
        if (stopped) {
            return Saver.Result.cancelled();
        }
        if (needGap) {
            Thread.sleep(GAP_MIN_MS + random.nextInt(GAP_JITTER_MS));
        }
        return Saver.save(this, url, index, skipSaved);
    }

    private void finishBatchItem(int batch, Saver.Result r) {
        Batch b = BATCHES.get(batch);
        if (b == null) {
            return;
        }
        boolean finished;
        int done;
        int saved;
        int skipped;
        int failed;
        boolean stopped;
        synchronized (b) {
            b.done++;
            if (r.skipped) {
                b.skipped++;
                b.lastWasNetwork = false; // 인스타그램에 요청하지 않았으니 간격이 필요 없음
            } else if (r.ok) {
                b.saved++;
                b.lastWasNetwork = true;
                b.lastUri = r.lastUri;
                b.lastMime = r.lastMime;
            } else if (r.cancelled) {
                b.failed++;
            } else {
                b.failed++;
                b.lastWasNetwork = true;
                if (r.shouldStopBatch()) {
                    b.stopped = true; // 계속하면 계정만 더 막히므로 남은 항목은 실행하지 않음
                }
            }
            finished = b.done >= b.total;
            done = b.done;
            saved = b.saved;
            skipped = b.skipped;
            failed = b.failed;
            stopped = b.stopped;
        }
        String message = summary(saved, skipped, failed, stopped, b.total);
        Listener l = listener;
        if (l != null) {
            l.onBatch(batch, done, b.total, saved, skipped, failed, stopped, finished, message);
        }
        if (finished) {
            BATCHES.remove(batch);
            if (!appVisible) {
                notifyDone(5000 + batch, stopped ? "저장을 멈췄어요" : "저장 완료", message, saved > 0, b.lastUri, b.lastMime);
            }
        }
    }

    private static String summary(int saved, int skipped, int failed, boolean stopped, int total) {
        StringBuilder sb = new StringBuilder();
        sb.append(saved).append("개 저장");
        if (skipped > 0) sb.append(", ").append(skipped).append("개는 이미 받아서 건너뜀");
        if (failed > 0) sb.append(", ").append(failed).append("개 실패");
        if (stopped) {
            sb.append(". 인스타그램이 요청을 제한하거나 로그인이 필요해서 남은 항목은 멈췄어요. 잠시 뒤 다시 시도해 주세요.");
        }
        return sb.toString();
    }

    private Notification progressNotification() {
        int total = Math.max(sessionTotal.get(), 1);
        String text = total > 1 ? "저장 중이에요… (" + sessionDone.get() + "/" + total + ")" : "저장 중이에요…";
        return new Notification.Builder(this, CH_PROGRESS)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("인스타 저장")
                .setContentText(text)
                .setProgress(total, Math.min(sessionDone.get(), total), total <= 1)
                .setOngoing(true)
                .setContentIntent(openAppIntent())
                .build();
    }

    private void notifyDone(int notificationId, String title, String message, boolean ok, Uri uri, String mime) {
        PendingIntent target = openAppIntent();
        if (ok && uri != null && mime != null) {
            // 알림을 누르면 방금 저장한 사진/영상을 엽니다.
            Intent view = new Intent(Intent.ACTION_VIEW)
                    .setDataAndType(uri, mime)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            target = PendingIntent.getActivity(this, notificationId, view, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        }
        Notification n = new Notification.Builder(this, CH_DONE)
                .setSmallIcon(ok ? android.R.drawable.stat_sys_download_done : android.R.drawable.stat_notify_error)
                .setContentTitle(title)
                .setContentText(message)
                .setStyle(new Notification.BigTextStyle().bigText(message))
                .setAutoCancel(true)
                .setContentIntent(target)
                .build();
        nm.notify(notificationId, n);
    }

    private PendingIntent openAppIntent() {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @Override
    public void onDestroy() {
        worker.shutdown();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
