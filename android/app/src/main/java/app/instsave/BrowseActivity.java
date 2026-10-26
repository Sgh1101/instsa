package app.instsave;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 인스타그램을 앱 안에서 열어 사람이 직접 넘겨 보는 화면. 게시물/릴스를 열면 "이 게시물 저장" 버튼이 떠서,
 * 누를 때마다 그 게시물 하나를 갤러리에 저장합니다.
 *
 * 앱이 계정의 게시물 목록을 대신 불러오거나 화면을 자동으로 훑지 않습니다. 화면에 보이는 것은 인스타그램이 사람에게
 * 보여 주는 그대로이고, 저장은 사람이 누른 게시물 하나씩만(공유 → 바로 저장과 같은 요청) 합니다.
 */
public class BrowseActivity extends Activity {
    private static final String HOME = "https://www.instagram.com/";
    /** /p/코드, /reel/코드, /tv/코드 (앞에 /계정이름 이 붙어도 됨). /reels/ 같은 목록 화면은 제외합니다. */
    private static final Pattern POST_PATH = Pattern.compile("^(?:/[A-Za-z0-9_.]+)?/(p|reel|tv)/([A-Za-z0-9_-]+)(?:/.*)?$");
    private static final Pattern PROFILE_PATH = Pattern.compile("^/([A-Za-z0-9_.]{1,30})/?$");
    /** 연달아 눌러도 인스타그램에 쏟아지지 않도록 저장 사이에 두는 최소 간격. */
    private static final long MIN_TAP_GAP_MS = 3000;

    private final ExecutorService bg = Executors.newSingleThreadExecutor();
    private WebView web;
    private Button saveButton;
    private TextView title;
    private PyObject api;

    private String code = "";
    private String kind = "";
    private boolean codeSaved;
    private int pendingBatch;
    private long lastTap;
    private String syncedSession = "";

    private final SaveService.Listener listener = new SaveService.Listener() {
        @Override
        public void onResult(int id, boolean ok, String message, String detail) {
            // 이 화면은 항상 묶음(1개)으로 저장하므로 낱개 결과는 쓰지 않습니다.
        }

        @Override
        public void onBatch(int batch, int done, int total, int saved, int skipped, int failed, boolean stopped,
                            boolean finished, String message) {
            if (!finished || batch != pendingBatch) return;
            runOnUiThread(() -> {
                pendingBatch = 0;
                Toast.makeText(BrowseActivity.this, message, Toast.LENGTH_LONG).show();
                setResult(RESULT_OK); // 돌아가면 메인 화면의 '최근 저장'을 새로 고칩니다
                refreshButton();
            });
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        api = PyHost.api(this);
        buildUi();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        // "; wv" 표식이 있으면 인스타그램이 앱 내 브라우저로 보고 막을 수 있어 일반 Chrome처럼 보이게 합니다(LoginActivity와 같음).
        s.setUserAgentString(WebSettings.getDefaultUserAgent(this).replace("; wv", ""));
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);
        web.setWebViewClient(new Client());

        String name = getIntent().getStringExtra("name");
        web.loadUrl(name != null && name.matches("[A-Za-z0-9_.]{1,30}") ? HOME + name + "/" : HOME);
        SaveService.browseListener = listener;
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        root.setFitsSystemWindows(true);

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(4), dp(2), dp(12), dp(2));
        Button close = new Button(this);
        close.setText("‹ 닫기");
        close.setAllCaps(false);
        close.setBackgroundColor(Color.TRANSPARENT);
        close.setTextColor(Color.parseColor("#262626"));
        close.setOnClickListener(v -> finish());
        title = new TextView(this);
        title.setTextColor(Color.parseColor("#262626"));
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        title.setText("인스타그램");
        bar.addView(close);
        bar.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        FrameLayout body = new FrameLayout(this);
        web = new WebView(this);
        body.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        saveButton = new Button(this);
        saveButton.setAllCaps(false);
        saveButton.setTextColor(Color.WHITE);
        saveButton.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        saveButton.setTypeface(null, android.graphics.Typeface.BOLD);
        saveButton.setPadding(dp(26), dp(10), dp(26), dp(10));
        GradientDrawable pill = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{Color.parseColor("#d62976"), Color.parseColor("#a02ab5"), Color.parseColor("#5b51d8")});
        pill.setCornerRadius(dp(28));
        saveButton.setBackground(pill);
        saveButton.setElevation(dp(8));
        saveButton.setVisibility(View.GONE);
        saveButton.setOnClickListener(v -> saveCurrent());
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(52));
        lp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
        lp.bottomMargin = dp(28);
        body.addView(saveButton, lp);

        root.addView(body, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics()));
    }

    /** 인스타그램 안에서만 이동합니다. 다른 사이트나 인스타그램 앱을 여는 주소는 열지 않습니다. */
    private static boolean allowed(Uri u) {
        String host = u.getHost();
        return "https".equals(u.getScheme()) && host != null && (host.equals("instagram.com") || host.endsWith(".instagram.com"));
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            if (!request.isForMainFrame() || allowed(request.getUrl())) {
                return false;
            }
            Toast.makeText(BrowseActivity.this, "인스타그램 밖의 주소는 여기서 열지 않아요", Toast.LENGTH_SHORT).show();
            return true;
        }

        // 인스타그램은 화면을 다시 불러오지 않고 주소만 바꾸는 경우가 많아서 이 콜백으로 주소 변화를 따라갑니다.
        @Override
        public void doUpdateVisitedHistory(WebView view, String url, boolean isReload) {
            onUrl(url);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            onUrl(url);
            syncLogin();
        }
    }

    private void onUrl(String url) {
        Uri u = url == null ? null : Uri.parse(url);
        if (u == null || !allowed(u)) return;
        String path = u.getPath() == null ? "/" : u.getPath();
        Matcher post = POST_PATH.matcher(path);
        if (post.matches()) {
            kind = post.group(1);
            code = post.group(2);
            title.setText("게시물");
            refreshButton();
            return;
        }
        code = "";
        kind = "";
        saveButton.setVisibility(View.GONE);
        Matcher profile = PROFILE_PATH.matcher(path);
        title.setText(profile.matches() ? "@" + profile.group(1) : "인스타그램");
    }

    /** 현재 게시물이 이미 저장됐는지에 따라 버튼 글자를 정합니다(인스타그램에 요청하지 않고 앱 기록만 봅니다). */
    private void refreshButton() {
        final String forCode = code;
        if (forCode.isEmpty()) {
            saveButton.setVisibility(View.GONE);
            return;
        }
        saveButton.setVisibility(View.VISIBLE);
        saveButton.setEnabled(pendingBatch == 0);
        saveButton.setText(pendingBatch != 0 ? "저장 중…" : codeSaved && forCode.equals(savedCode) ? "다시 저장" : "이 게시물 저장");
        if (pendingBatch == 0) {
            bg.submit(() -> {
                boolean saved;
                try {
                    saved = api.callAttr("is_saved", forCode).toBoolean();
                } catch (Exception e) {
                    return; // 표시용이라 실패해도 기본 글자를 유지
                }
                runOnUiThread(() -> {
                    if (forCode.equals(code) && pendingBatch == 0) {
                        savedCode = forCode;
                        codeSaved = saved;
                        saveButton.setText(saved ? "다시 저장" : "이 게시물 저장");
                    }
                });
            });
        }
    }

    private String savedCode = "";

    private void saveCurrent() {
        if (code.isEmpty() || pendingBatch != 0) return;
        long now = SystemClock.elapsedRealtime();
        if (lastTap != 0 && now - lastTap < MIN_TAP_GAP_MS) {
            Toast.makeText(this, "잠깐만요, 곧 다시 누를 수 있어요", Toast.LENGTH_SHORT).show();
            return;
        }
        lastTap = now;
        Permissions.askNotificationsOnce(this);
        // 사람이 한 번 누른 게시물 하나 = 공유 → 바로 저장과 같은 요청입니다. 표준 주소로 바꿔서 보냅니다.
        pendingBatch = SaveService.registerBatch(1);
        SaveService.enqueueInBatch(this, pendingBatch, HOME + kind + "/" + code + "/", false);
        Toast.makeText(this, "저장을 시작했어요. 계속 둘러봐도 돼요", Toast.LENGTH_SHORT).show();
        refreshButton();
    }

    /**
     * 이 화면에서 로그인했다면 로그인 쿠키를 앱 저장소에도 복사해, 저장할 때 같은 로그인을 쓰게 합니다
     * (LoginActivity가 하는 일과 같음). 값이 바뀔 때만 한 번 저장합니다.
     */
    private void syncLogin() {
        String cookies = CookieManager.getInstance().getCookie("https://www.instagram.com");
        if (cookies == null) return;
        String session = LoginActivity.cookieValue(cookies, "sessionid");
        if (session.isEmpty() || session.equals(syncedSession)) return;
        syncedSession = session;
        String csrf = LoginActivity.cookieValue(cookies, "csrftoken");
        bg.submit(() -> {
            try {
                api.callAttr("save_cookies", session, csrf);
                runOnUiThread(() -> setResult(RESULT_OK));
            } catch (Exception ignored) {
                syncedSession = ""; // 다음 페이지에서 다시 시도
            }
        });
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
        if (SaveService.browseListener == listener) {
            SaveService.browseListener = null;
        }
        bg.shutdown();
        if (web != null) {
            web.destroy();
        }
        CookieManager.getInstance().flush();
        super.onDestroy();
    }
}
