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

        Result(boolean ok, String message, Uri lastUri, String lastMime) {
            this.ok = ok;
            this.message = message;
            this.lastUri = lastUri;
            this.lastMime = lastMime;
        }
    }

    private Saver() {}

    /** index가 0이면 전부, 아니면 해당 번호(1부터) 항목만. 블로킹 호출이라 백그라운드 스레드에서 부르세요. */
    static Result save(Context context, String url, int index) {
        File dir = null;
        try {
            PyObject api = PyHost.api(context);
            String json = api.callAttr("save", url, index, context.getCacheDir().getAbsolutePath()).toString();
            JSONObject o = new JSONObject(json);
            if (o.has("error")) {
                return new Result(false, o.getString("error"), null, null);
            }
            dir = new File(o.getString("dir"));
            JSONArray files = o.getJSONArray("files");
            Uri last = null;
            String lastMime = null;
            for (int i = 0; i < files.length(); i++) {
                JSONObject f = files.getJSONObject(i);
                String mime = f.getString("mime");
                last = saveToGallery(context, new File(f.getString("path")), f.getString("name"), mime);
                lastMime = mime;
            }
            return new Result(true, files.length() + "개를 갤러리에 저장했어요", last, lastMime);
        } catch (Exception e) {
            return new Result(false, "저장하지 못했어요: " + e.getMessage(), null, null);
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
