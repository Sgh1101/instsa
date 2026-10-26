package app.instsave;

import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

/** 알림 권한(안드로이드 13+)을 한 번만 묻습니다. 거절해도 저장은 되고, 진행/완료 알림만 안 보입니다. */
final class Permissions {
    private static final int REQUEST_NOTIFICATIONS = 100;

    private Permissions() {}

    /** 실제로 권한 창을 띄웠으면 true. 이미 허용했거나 예전에 물어봤거나 필요 없는 버전이면 false. */
    static boolean askNotificationsOnce(Activity activity) {
        if (Build.VERSION.SDK_INT < 33) return false;
        if (activity.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        SharedPreferences prefs = activity.getSharedPreferences("app", Activity.MODE_PRIVATE);
        if (prefs.getBoolean("asked_notifications", false)) return false;
        prefs.edit().putBoolean("asked_notifications", true).apply();
        activity.requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFICATIONS);
        return true;
    }
}
