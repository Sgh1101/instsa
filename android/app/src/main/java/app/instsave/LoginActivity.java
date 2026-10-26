package app.instsave;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.PyObject;

import org.json.JSONObject;

/**
 * 인스타그램 로그인 화면을 열고, 로그인되면 sessionid 쿠키를 가져와 저장합니다.
 * PC에서 쿠키를 복사할 필요가 없습니다. 비밀번호는 이 앱이 보지 않고(인스타그램 페이지가 직접 받음), 쿠키만 가져옵니다.
 */
public class LoginActivity extends Activity {
    private static final String SITE = "https://www.instagram.com";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private boolean done;

    private final Runnable poll = new Runnable() {
        @Override
        public void run() {
            if (done) return;
            check();
            handler.postDelayed(this, 1500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        // "; wv" 표식이 있으면 인스타그램이 앱 내 브라우저로 보고 로그인을 막을 수 있어 일반 Chrome처럼 보이게 합니다.
        s.setUserAgentString(WebSettings.getDefaultUserAgent(this).replace("; wv", ""));

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient());
        web.loadUrl(SITE + "/accounts/login/");
        // 로그인 뒤 화면 전환은 페이지 새로고침 없이 일어나서, 주기적으로 쿠키를 확인합니다.
        handler.postDelayed(poll, 1500);
    }

    private void check() {
        String cookies = CookieManager.getInstance().getCookie(SITE);
        if (cookies == null) return;
        String sessionId = cookieValue(cookies, "sessionid");
        if (sessionId.isEmpty()) return;

        done = true;
        String csrf = cookieValue(cookies, "csrftoken");
        new Thread(() -> {
            String message;
            boolean ok = false;
            try {
                PyObject api = PyHost.api(this);
                JSONObject r = new JSONObject(api.callAttr("save_cookies", sessionId, csrf).toString());
                ok = r.optBoolean("ok");
                message = ok ? "로그인했어요" : r.optString("error", "로그인 정보를 저장하지 못했어요");
            } catch (Exception e) {
                message = "로그인 정보를 저장하지 못했어요: " + e.getMessage();
            }
            String finalMessage = message;
            boolean finalOk = ok;
            runOnUiThread(() -> {
                Toast.makeText(this, finalMessage, Toast.LENGTH_LONG).show();
                if (finalOk) {
                    // WebView의 로그인 상태는 남겨 둡니다: '앱 안에서 둘러보기'(BrowseActivity)가 같은 로그인을 씁니다.
                    // 로그인 해제를 누르면 MainActivity가 이 쿠키도 함께 지웁니다.
                    CookieManager.getInstance().flush();
                    setResult(RESULT_OK);
                }
                finish();
            });
        }).start();
    }

    static String cookieValue(String cookieHeader, String name) {
        for (String part : cookieHeader.split(";")) {
            String p = part.trim();
            if (p.startsWith(name + "=")) {
                return p.substring(name.length() + 1);
            }
        }
        return "";
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        done = true;
        handler.removeCallbacks(poll);
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }
}
