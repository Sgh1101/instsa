package app.instsave;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;

import com.chaquo.python.PyObject;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/** 게시물을 받아(파이썬) 갤러리에 저장(MediaStore)합니다. 화면과 무관해서 서비스에서 돌립니다. */
final class Saver {
    static final String GALLERY_FOLDER = "InstaSave";

    static final class Result {
        final boolean ok;
        final String message;
        final Uri lastUri;     // 알림을 눌렀을 때 열어 줄 마지막 파일
        final String lastMime;
        final int status;      // 실패 원인 코드(429: 요청 제한, 401: 로그인 필요 등). 없으면 0
        final boolean skipped; // 이미 받은 게시물이라 건너뜀(인스타그램에 요청하지 않음)
        final boolean cancelled; // 앞선 실패로 묶음이 중단되어 실행하지 않음
        final String detail;   // 실패 원인의 원문(진단용). 화면의 '자세히'에 보입니다

        Result(boolean ok, String message, Uri lastUri, String lastMime, int status, boolean skipped, boolean cancelled,
               String detail) {
            this.ok = ok;
            this.message = message;
            this.lastUri = lastUri;
            this.lastMime = lastMime;
            this.status = status;
            this.skipped = skipped;
            this.cancelled = cancelled;
            this.detail = detail == null ? "" : detail;
        }

        static Result ok(String message, Uri uri, String mime) {
            return new Result(true, message, uri, mime, 0, false, false, "");
        }

        static Result failed(String message, int status, String detail) {
            return new Result(false, message, null, null, status, false, false, detail);
        }

        static Result skipped() {
            return new Result(true, "이미 저장한 게시물이라 건너뛰었어요", null, null, 0, true, false, "");
        }

        static Result cancelled() {
            return new Result(false, "앞선 오류로 멈췄어요", null, null, 0, false, true, "");
        }

        /** 요청 제한이거나 로그인이 필요해서, 같은 묶음의 나머지를 계속하면 계정만 더 막힐 상황. */
        boolean shouldStopBatch() {
            return !ok && (status == 429 || status == 401);
        }
    }

    private Saver() {}

    /**
     * index가 0이면 전부, 아니면 해당 번호(1부터) 항목만. skipSaved면 이미 받은 게시물은 건너뜁니다.
     * 블로킹 호출이라 백그라운드 스레드에서 부르세요.
     */
    static Result save(Context context, String url, int index, boolean skipSaved) {
        File dir = null;
        try {
            PyObject api = PyHost.api(context);
            String json = api.callAttr("save", url, index, context.getCacheDir().getAbsolutePath(), skipSaved).toString();
            JSONObject o = new JSONObject(json);
            if (o.optBoolean("skipped")) {
                return Result.skipped();
            }
            if (o.has("error")) {
                return Result.failed(o.getString("error"), o.optInt("status", 0), o.optString("detail"));
            }
            dir = new File(o.getString("dir"));
            JSONArray files = o.getJSONArray("files");
            Uri last = null;
            String lastMime = null;
            Uri first = null;
            String firstMime = null;
            for (int i = 0; i < files.length(); i++) {
                JSONObject f = files.getJSONObject(i);
                String mime = f.getString("mime");
                last = saveToGallery(context, new File(f.getString("path")), f.getString("name"), mime);
                lastMime = mime;
                if (first == null) {
                    first = last;
                    firstMime = mime;
                }
            }
            if (index == 0) {
                // 갤러리 저장까지 끝난 게시물만 '저장한 게시물'로 기록합니다(다음 묶음 저장에서 건너뛰고, 프로필 목록에 올라감).
                // 미리보기 주소·개수·종류·첫 파일 위치도 함께 남겨 '최근 저장' 목록에서 쓰입니다.
                api.callAttr("mark_saved", o.optString("code"), o.optString("uploader"), o.optString("thumb_url"),
                        files.length(), o.optString("kind"), first == null ? "" : first.toString(),
                        firstMime == null ? "" : firstMime);
            }
            return Result.ok(files.length() + "개를 갤러리에 저장했어요", last, lastMime);
        } catch (Exception e) {
            // 갤러리 저장 실패 등 자바 쪽 오류도 파이썬의 '최근 오류' 기록에 남겨 진단에서 함께 보이게 합니다.
            String detail = e.toString();
            try {
                PyHost.api(context).callAttr("log_error", "저장", url, "저장하지 못했어요", detail);
            } catch (Exception ignored) {
                // 기록은 부가 기능이라 실패해도 무시
            }
            return Result.failed("저장하지 못했어요: " + e.getMessage(), 0, detail);
        } finally {
            if (dir != null) {
                deleteRecursively(dir);
            }
        }
    }

    /** Android 10+ MediaStore: 저장소 권한 없이 갤러리(사진/동영상)에 저장합니다. */
    private static Uri saveToGallery(Context context, File file, String displayName, String mime) throws IOException {
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

        ContentResolver resolver = context.getContentResolver();
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
        return uri;
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
}
