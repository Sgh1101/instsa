package app.instsave;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.IBinder;
import android.os.PowerManager;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 저장을 앱 화면과 별개로 돌리는 포그라운드 서비스. 알림에 "저장 중…"이 떠 있는 동안에는
 * 사용자가 앱을 나가거나 화면을 꺼도 안드로이드가 프로세스를 끊지 않습니다.
 * 요청은 한 번에 하나씩 순서대로 처리합니다(인스타그램 요청 제한을 덜 건드리기 위해).
 */
public class SaveService extends Service {
    /** 화면이 떠 있을 때 결과를 화면(JS)으로 돌려주는 통로. 화면이 없으면 null. */
    interface Listener {
        void onResult(int id, boolean ok, String message);
    }

    static volatile Listener listener;
    /** 앱이 화면에 보이는 중인가. 보이면 결과를 토스트로 알리므로 완료 알림은 생략합니다. */
    static volatile boolean appVisible;

    private static final String CH_PROGRESS = "saving";
    private static final String CH_DONE = "done";
    private static final int FGS_ID = 1;
    private static final long WAKE_LOCK_MS = 10 * 60 * 1000L;

    private final AtomicInteger pending = new AtomicInteger();
    private ExecutorService worker;
    private NotificationManager nm;

    /** 앱이 화면에 있을 때 불러야 합니다(안드로이드 12+는 백그라운드에서 포그라운드 서비스를 시작할 수 없음). */
    static void enqueue(Context context, int id, String url, int index) {
        Intent i = new Intent(context, SaveService.class)
                .putExtra("id", id)
                .putExtra("url", url)
                .putExtra("index", index);
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

        pending.incrementAndGet();
        // 서비스를 시작한 뒤 5초 안에 불러야 합니다. 안드로이드 14+는 유형(dataSync) 지정이 필수입니다.
        startForeground(FGS_ID, progressNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        worker.submit(() -> run(id, url, index));
        return START_NOT_STICKY;
    }

    private void run(int id, String url, int index) {
        PowerManager.WakeLock wakeLock = getSystemService(PowerManager.class)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "instasave:save");
        wakeLock.acquire(WAKE_LOCK_MS); // 화면이 꺼져도 CPU가 잠들지 않게(시간 제한 있음)
        Saver.Result r;
        try {
            r = Saver.save(this, url, index);
        } catch (Throwable t) {
            r = new Saver.Result(false, "저장하지 못했어요: " + t.getMessage(), null, null);
        } finally {
            if (wakeLock.isHeld()) {
                wakeLock.release();
            }
        }

        Listener l = listener;
        if (l != null) {
            l.onResult(id, r.ok, r.message);
        }
        if (!appVisible) {
            notifyDone(id, r);
        }
        if (pending.decrementAndGet() == 0) {
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    private Notification progressNotification() {
        return new Notification.Builder(this, CH_PROGRESS)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("인스타 저장")
                .setContentText("저장 중이에요…")
                .setProgress(0, 0, true)
                .setOngoing(true)
                .setContentIntent(openAppIntent())
                .build();
    }

    private void notifyDone(int id, Saver.Result r) {
        PendingIntent target = openAppIntent();
        if (r.ok && r.lastUri != null && r.lastMime != null) {
            // 알림을 누르면 방금 저장한 사진/영상을 엽니다.
            Intent view = new Intent(Intent.ACTION_VIEW)
                    .setDataAndType(r.lastUri, r.lastMime)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            target = PendingIntent.getActivity(this, id, view, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        }
        Notification n = new Notification.Builder(this, CH_DONE)
                .setSmallIcon(r.ok ? android.R.drawable.stat_sys_download_done : android.R.drawable.stat_notify_error)
                .setContentTitle(r.ok ? "저장 완료" : "저장 실패")
                .setContentText(r.message)
                .setStyle(new Notification.BigTextStyle().bigText(r.message))
                .setAutoCancel(true)
                .setContentIntent(target)
                .build();
        nm.notify(100 + id, n);
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
