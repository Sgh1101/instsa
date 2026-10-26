package app.instsave;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Toast;

/**
 * 공유 시트의 "바로 저장". 앱 화면을 열지 않고 백그라운드 저장만 시작한 뒤 바로 닫혀서,
 * 사용자는 인스타그램 화면에 그대로 머뭅니다. 눈에 보이는 화면이 없는 투명 Activity입니다.
 *
 * 사람이 게시물을 하나씩 공유하는 것과 같은 요청이라 자동 조회가 아닙니다(인스타그램을 몰래 훑지 않음).
 * 링크가 여러 개면 묶음으로(간격을 두고, 이미 받은 것은 건너뜀) 처리하고, 한 번에 최대 MAX_BATCH개입니다.
 */
public class QuickSaveActivity extends Activity {
    private boolean waitingForPermission;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent intent = getIntent();
        String text = intent == null ? null : intent.getStringExtra(Intent.EXTRA_TEXT);
        String[] links = MainActivity.allLinks(text).split("\n");
        if (text == null || links.length == 0 || links[0].isEmpty()) {
            Toast.makeText(this, "인스타그램 게시물 링크가 아니에요", Toast.LENGTH_SHORT).show();
            finish();
            return;
        }

        int n = Math.min(links.length, MainActivity.MAX_BATCH);
        // 한 개여도 묶음으로 처리합니다: 이미 받은 게시물은 건너뛰고, 끝나면 알림으로 요약해 줍니다.
        int batch = SaveService.registerBatch(n);
        for (int i = 0; i < n; i++) {
            SaveService.enqueueInBatch(this, batch, links[i], true);
        }
        String extra = links.length > n ? " (한 번에 " + n + "개까지만 저장해요)" : "";
        Toast.makeText(this, n + "개 저장을 시작했어요. 끝나면 알림으로 알려 드려요" + extra, Toast.LENGTH_SHORT).show();

        // 처음 한 번만 알림 권한을 묻습니다. 권한 창이 닫힐 때까지 Activity를 유지해야 창이 사라지지 않습니다.
        waitingForPermission = Permissions.askNotificationsOnce(this);
        if (!waitingForPermission) {
            finish();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        finish();
    }
}
