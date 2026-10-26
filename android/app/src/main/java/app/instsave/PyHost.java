package app.instsave;

import android.content.Context;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;

/** 앱 안에서 파이썬을 한 번만 시작하고 android_api 모듈을 돌려줍니다. */
final class PyHost {
    private static boolean inited;

    private PyHost() {}

    static synchronized PyObject api(Context context) {
        Context app = context.getApplicationContext();
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(app));
        }
        PyObject module = Python.getInstance().getModule("android_api");
        if (!inited) {
            // 로그인 쿠키 등 앱 데이터는 앱 전용 폴더(다른 앱이 읽을 수 없음)에 저장됩니다.
            module.callAttr("init", new File(app.getFilesDir(), "igdl").getAbsolutePath());
            inited = true;
        }
        return module;
    }
}
