package app.instsave;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
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
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
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
    private static final String GALLERY_FOLDER = "InstaSave";

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
            pool.submit(() -> doSave(id, url, index));
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

    // ---- 갤러리 저장 -------------------------------------------------------------

    private void doSave(int id, String url, int index) {
        File dir = null;
        try {
            String json = api.callAttr("save", url, index, getCacheDir().getAbsolutePath()).toString();
            JSONObject o = new JSONObject(json);
            if (o.has("error")) {
                saved(id, false, o.getString("error"));
                return;
            }
            dir = new File(o.getString("dir"));
            JSONArray files = o.getJSONArray("files");
            for (int i = 0; i < files.length(); i++) {
                JSONObject f = files.getJSONObject(i);
                saveToGallery(new File(f.getString("path")), f.getString("name"), f.getString("mime"));
            }
            saved(id, true, files.length() + "개를 갤러리에 저장했어요");
        } catch (Exception e) {
            saved(id, false, "저장하지 못했어요: " + e.getMessage());
        } finally {
            if (dir != null) {
                deleteRecursively(dir);
            }
        }
    }

    /** Android 10+ MediaStore: 저장소 권한 없이 갤러리(사진/동영상)에 저장합니다. */
    private void saveToGallery(File file, String displayName, String mime) throws IOException {
        boolean video = mime.startsWith("video/");
        Uri collection = video
                ? MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                : MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY);
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, displayName);
        values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
        values.put(MediaStore.MediaColumns.RELATIVE_PATH,
                (video ? Environment.DIRECTORY_MOVIES : Environment.DIRECTORY_PICTURES) + "/" + GALLERY_FOLDER);
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);

        ContentResolver resolver = getContentResolver();
        Uri uri = resolver.insert(collection, values);
        if (uri == null) {
            throw new IOException("갤러리에 파일을 만들지 못했어요");
        }
        try (InputStream in = new FileInputStream(file); OutputStream out = resolver.openOutputStream(uri)) {
            if (out == null) {
                throw new IOException("갤러리 파일을 열지 못했어요");
            }
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        } catch (IOException e) {
            resolver.delete(uri, null, null); // 반쯤 쓴 파일이 갤러리에 남지 않도록
            throw e;
        }
        values.clear();
        values.put(MediaStore.MediaColumns.IS_PENDING, 0);
        resolver.update(uri, values, null, null);
    }

    private static void deleteRecursively(File f) {
        File[] children = f.listFiles();
        if (children != null) {
            for (File c : children) {
                deleteRecursively(c);
            }
        }
        // 임시 파일 정리라서 실패해도 무시합니다.
        f.delete();
    }

    // ---- 클립보드 / 공유 ---------------------------------------------------------

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        // Android 10+는 앱이 화면 앞에 있을 때만 클립보드를 읽을 수 있습니다.
        if (hasFocus) {
            Matcher m = IG_LINK.matcher(clipboardString());
            if (m.find()) {
                deliver(m.group(), "clipboard"); // 같은 링크 반복 처리는 화면(JS)이 막습니다
            }
        }
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
        Matcher m = IG_LINK.matcher(text);
        if (m.find()) {
            deliver(m.group(), "share");
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
    protected void onDestroy() {
        pool.shutdown();
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }
}
