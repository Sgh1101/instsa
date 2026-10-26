package app.instsave;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.PyObject;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 화면은 WebView(앱에 들어 있는 HTML)이고, 화면의 /api/* 요청은 서버 없이 앱 안의 파이썬이 처리합니다.
 * 공유 시트, 클립보드, 갤러리 저장, 인스타그램 로그인처럼 안드로이드가 해야 하는 일은 여기서 합니다.
 */
public class MainActivity extends Activity {
    /** 가상의 주소입니다. 네트워크로 나가지 않고 전부 이 앱이 응답합니다. */
    static final String HOST = "app.local";
    private static final int REQ_LOGIN = 1;
    /** 한 번에 쌓을 수 있는 링크 수. 너무 많이 한꺼번에 요청하면 인스타그램이 계정을 제한할 수 있습니다. */
    static final int MAX_BATCH = 30;

    private static final Pattern IG_LINK = Pattern.compile(
            "https?://(?:www\\.|m\\.)?instagram\\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reels?|tv)/[A-Za-z0-9_-]+/?(?:\\?[^\\s]*)?",
            Pattern.CASE_INSENSITIVE);

    private final ExecutorService pool = Executors.newFixedThreadPool(3);
    private WebView web;
    private PyObject api;
    private boolean pageReady;
    private String pendingLink;
    private String pendingSource;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        api = PyHost.api(this);
        // 첫 조회가 느리지 않도록 무거운 import를 미리 해 둡니다.
        pool.submit(() -> {
            try {
                api.callAttr("warmup");
            } catch (Exception ignored) {
                // 실패해도 첫 조회 때 다시 시도됩니다.
            }
        });

        web = new WebView(this);
        setContentView(web);
        // 서비스가 저장을 끝내면 화면(JS)에 알려 토스트를 띄웁니다. 화면이 없으면 서비스가 알림으로 알립니다.
        SaveService.listener = new SaveService.Listener() {
            @Override
            public void onResult(int id, boolean ok, String message) {
                if (!isDestroyed()) {
                    saved(id, ok, message);
                }
            }

            @Override
            public void onBatch(int batch, int done, int total, int saved, int skipped, int failed, boolean stopped,
                                boolean finished, String message) {
                if (!isDestroyed()) {
                    js("window.__batch && window.__batch(" + batch + "," + done + "," + total + "," + saved + "," + skipped + ","
                            + failed + "," + stopped + "," + finished + "," + JSONObject.quote(message) + ")");
                }
            }
        };
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setWebViewClient(new Client());
        // WebChromeClient가 없으면 화면의 confirm()이 항상 "취소"가 되어 '로그인 해제' 같은 버튼이 동작하지 않습니다.
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton("확인", (d, w) -> result.confirm())
                        .setNegativeButton("취소", (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });
        web.loadUrl("https://" + HOST + "/");

        handleIntent(getIntent());
    }

    // ---- 화면 요청 처리 ------------------------------------------------------

    private class Client extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (!HOST.equals(u.getHost())) {
                return text(403, "blocked"); // 이 화면은 바깥 주소로 나갈 일이 없습니다
            }
            String path = u.getPath() == null ? "/" : u.getPath();
            if (path.startsWith("/api/")) {
                return apiResponse(request.getMethod(), path, u.getEncodedQuery());
            }
            return asset(path);
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return !HOST.equals(request.getUrl().getHost());
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            pageReady = false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            pageReady = true;
            String link = pendingLink;
            String source = pendingSource;
            pendingLink = null;
            pendingSource = null;
            if (link != null) {
                deliver(link, source);
            }
        }
    }

    private WebResourceResponse apiResponse(String method, String path, String query) {
        try {
            List<PyObject> r = api.callAttr("handle", method, path, query == null ? "" : query).asList();
            return response(r.get(0).toInt(), r.get(1).toString(), r.get(2).toJava(byte[].class));
        } catch (Exception e) {
            return text(500, "{\"error\":\"앱 내부 오류가 났어요.\"}");
        }
    }

    private WebResourceResponse asset(String path) {
        if (path.contains("..")) {
            return text(404, "not found");
        }
        String name = path.equals("/") ? "web/static/index.html" : "web" + path;
        try {
            InputStream in = getAssets().open(name);
            return new WebResourceResponse(mimeFor(name), isText(mimeFor(name)) ? "utf-8" : null, 200, "OK", noStore(), in);
        } catch (FileNotFoundException e) {
            return text(404, "not found");
        } catch (IOException e) {
            return text(500, "error");
        }
    }

    private static Map<String, String> noStore() {
        Map<String, String> h = new HashMap<>();
        h.put("Cache-Control", "no-store");
        return h;
    }

    private static WebResourceResponse response(int status, String contentType, byte[] body) {
        String mime = contentType.split(";")[0].trim();
        return new WebResourceResponse(mime, isText(mime) ? "utf-8" : null, status, reason(status), noStore(),
                new ByteArrayInputStream(body));
    }

    private static WebResourceResponse text(int status, String body) {
        return response(status, "text/plain", body.getBytes(StandardCharsets.UTF_8));
    }

    private static boolean isText(String mime) {
        return mime.startsWith("text/") || mime.equals("application/json") || mime.contains("javascript")
                || mime.contains("manifest") || mime.contains("svg");
    }

    private static String mimeFor(String name) {
        if (name.endsWith(".html")) return "text/html";
        if (name.endsWith(".js")) return "text/javascript";
        if (name.endsWith(".css")) return "text/css";
        if (name.endsWith(".svg")) return "image/svg+xml";
        if (name.endsWith(".png")) return "image/png";
        if (name.endsWith(".webmanifest") || name.endsWith(".json")) return "application/manifest+json";
        return "application/octet-stream";
    }

    private static String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 429: return "Too Many Requests";
            case 500: return "Internal Server Error";
            case 502: return "Bad Gateway";
            default: return status >= 500 ? "Server Error" : "Error";
        }
    }

    // ---- 화면 ↔ 앱 --------------------------------------------------------------

    /** 화면(JavaScript)이 부르는 기능. 이 WebView는 앱에 들어 있는 화면만 열기 때문에 노출해도 안전합니다. */
    private class Bridge {
        @JavascriptInterface
        public void save(int id, String url, int index) {
            runOnUiThread(() -> {
                Permissions.askNotificationsOnce(MainActivity.this);
                // 서비스가 저장하므로 앱을 나가도 계속됩니다. 앱이 화면에 있는 지금 시작해야 합니다(안드로이드 12+).
                SaveService.enqueue(MainActivity.this, id, url, index);
            });
        }

        /** 링크 여러 개를 한꺼번에 백그라운드 저장. 묶음 번호를 돌려줍니다(0이면 시작 못 함). */
        @JavascriptInterface
        public int saveBatch(String urlsJson, boolean skipSaved) {
            try {
                JSONArray urls = new JSONArray(urlsJson);
                int n = Math.min(urls.length(), MAX_BATCH);
                if (n == 0) {
                    return 0;
                }
                runOnUiThread(() -> Permissions.askNotificationsOnce(MainActivity.this));
                int batch = SaveService.registerBatch(n);
                for (int i = 0; i < n; i++) {
                    SaveService.enqueueInBatch(MainActivity.this, batch, urls.getString(i), skipSaved);
                }
                return batch;
            } catch (Exception e) {
                return 0;
            }
        }

        /** '저장한 프로필'에서 지우기. 이 계정의 저장 기록(이미 받음 표시 포함)을 지웁니다. 갤러리의 파일은 그대로입니다. */
        @JavascriptInterface
        public void removeProfile(String name) {
            pool.submit(() -> {
                api.callAttr("remove_profile", name);
                js("window.__profilesChanged && window.__profilesChanged()");
            });
        }

        /** 인스타그램 앱(없으면 브라우저)으로 프로필을 엽니다. 거기서 게시물을 공유하면 이 앱으로 들어옵니다. */
        @JavascriptInterface
        public void openProfile(String name) {
            if (name == null || !name.matches("[A-Za-z0-9_.]{1,30}")) {
                return; // 인스타그램 계정 이름 형식만 허용
            }
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://www.instagram.com/" + name + "/")));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "프로필을 열 수 있는 앱이 없어요", Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void login() {
            runOnUiThread(() -> startActivityForResult(new Intent(MainActivity.this, LoginActivity.class), REQ_LOGIN));
        }

        @JavascriptInterface
        public void logout() {
            pool.submit(() -> {
                api.callAttr("clear_cookies");
                js("window.__cookieChanged && window.__cookieChanged()");
            });
        }

        @JavascriptInterface
        public String clipboardText() {
            return clipboardString();
        }
    }

    private void js(String code) {
        runOnUiThread(() -> web.evaluateJavascript(code, null));
    }

    private void saved(int id, boolean ok, String message) {
        js("window.__saved && window.__saved(" + id + "," + ok + "," + JSONObject.quote(message) + ")");
    }

    private void deliver(String link, String source) {
        if (pageReady) {
            js("window.__nativeLink && window.__nativeLink(" + JSONObject.quote(link) + "," + JSONObject.quote(source) + ")");
        } else {
            pendingLink = link;
            pendingSource = source;
        }
    }

    // ---- 클립보드 / 공유 ---------------------------------------------------------

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        // Android 10+는 앱이 화면 앞에 있을 때만 클립보드를 읽을 수 있습니다.
        if (hasFocus) {
            String links = allLinks(clipboardString());
            if (!links.isEmpty()) {
                deliver(links, "clipboard"); // 같은 링크 반복 처리는 화면(JS)이 막습니다
            }
        }
    }

    /** 글 속의 인스타그램 링크를 모두(중복 제거) 줄바꿈으로 이어 돌려줍니다. 없으면 빈 문자열. */
    static String allLinks(String text) {
        Matcher m = IG_LINK.matcher(text == null ? "" : text);
        java.util.LinkedHashSet<String> links = new java.util.LinkedHashSet<>();
        while (m.find()) {
            links.add(m.group());
        }
        return String.join("\n", links);
    }

    private String clipboardString() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip()) return "";
            ClipData d = cm.getPrimaryClip();
            if (d == null || d.getItemCount() == 0) return "";
            CharSequence t = d.getItemAt(0).coerceToText(this);
            return t == null ? "" : t.toString();
        } catch (Exception e) {
            return "";
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    /** 인스타그램의 공유 → "인스타 저장"으로 들어온 링크. */
    private void handleIntent(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return;
        String text = intent.getStringExtra(Intent.EXTRA_TEXT);
        if (text == null) return;
        String links = allLinks(text);
        if (!links.isEmpty()) {
            deliver(links, "share");
        } else {
            Toast.makeText(this, "인스타그램 게시물 링크가 아니에요", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_LOGIN) {
            js("window.__cookieChanged && window.__cookieChanged()");
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        SaveService.appVisible = true;
    }

    @Override
    protected void onStop() {
        SaveService.appVisible = false;
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        SaveService.listener = null;
        pool.shutdown();
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }
}
