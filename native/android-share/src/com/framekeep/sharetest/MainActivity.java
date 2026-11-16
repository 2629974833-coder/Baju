package com.framekeep.sharetest;

import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private TextView status;
    private Button button;
    private boolean busy;
    private static final int PICK = 10, SHARE = 11;
    private static final long LIMIT = 128L * 1024 * 1024;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(32, 64, 32, 32);
        root.setBackgroundColor(Color.rgb(13, 23, 17));
        TextView heading = text("拾帧 · 直接转微信测试", 25);
        root.addView(heading);
        root.addView(text("只测试本地 MP4 视频，不需要小程序账号。\n不读取联系人，不自动选择好友或发送。", 16));
        status = text("点击下方按钮，选取一段你有权分享的视频。随后尝试直接打开微信的视频转发界面。", 16);
        root.addView(status);
        button = new Button(this);
        button.setText("分享给微信好友");
        button.setOnClickListener(view -> pick());
        root.addView(button);
        root.addView(text("当前不连接网页素材库，不执行去水印。视频大小上限约 134 MB。\n如果微信出现文件而不是可直接播放的视频，请如实反馈，不代表测试已通过。", 14));
        setContentView(root);
        if (Intent.ACTION_SEND.equals(getIntent().getAction())) {
            Uri source = getIntent().getParcelableExtra(Intent.EXTRA_STREAM);
            if (source != null) prepare(source);
        }
    }
    private TextView text(String value, int size) {
        TextView label = new TextView(this);
        label.setText(value); label.setTextSize(size); label.setTextColor(Color.rgb(230, 239, 230));
        label.setPadding(0, 20, 0, 20); return label;
    }
    private void pick() {
        if (busy) return;
        Intent request = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        request.setType("video/mp4"); request.addCategory(Intent.CATEGORY_OPENABLE);
        try { startActivityForResult(request, PICK); }
        catch (RuntimeException error) { status.setText("此手机没有可用的视频选择器。"); }
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == PICK) {
            if (result == RESULT_OK && data != null && data.getData() != null) prepare(data.getData());
            else status.setText("已取消选取，没有分享。");
        } else if (request == SHARE) {
            busy = false; button.setEnabled(true);
            status.setText("已从微信返回。是否已发送、视频能否直接播放，请以实际聊天记录为准。");
        }
    }
    private void prepare(Uri source) {
        if (busy) return;
        if (!"content".equals(source.getScheme())) { status.setText("只接受你选取的本地视频，不接受网址或任意文件路径。"); return; }
        busy = true; button.setEnabled(false); status.setText("正在准备视频，完成后交给微信…");
        worker.execute(() -> {
            File output = null;
            try {
                String mime = getContentResolver().getType(source);
                if (!"video/mp4".equals(mime)) throw new IllegalArgumentException("本次只测试 MP4 视频，请换一段视频。");
                File directory = new File(getCacheDir(), "shared");
                if (!directory.isDirectory() && !directory.mkdirs()) throw new IllegalStateException("无法创建临时目录。");
                output = new File(directory, UUID.randomUUID().toString().replace("-", "") + ".mp4");
                long total = 0;
                try (InputStream input = getContentResolver().openInputStream(source); FileOutputStream target = new FileOutputStream(output)) {
                    if (input == null) throw new IllegalStateException("无法读取选取的视频。");
                    byte[] bytes = new byte[64 * 1024]; int count;
                    while ((count = input.read(bytes)) != -1) {
                        total += count;
                        if (total > LIMIT || Thread.currentThread().isInterrupted()) throw new IllegalArgumentException("视频超过测试大小上限，或准备已取消。");
                        target.write(bytes, 0, count);
                    }
                }
                if (total == 0) throw new IllegalArgumentException("选取的视频为空。");
                final Uri local = Uri.parse("content://com.framekeep.sharetest.video/" + output.getName());
                runOnUiThread(() -> openWechat(local));
            } catch (Exception error) {
                if (output != null) output.delete();
                runOnUiThread(() -> { if (!isDestroyed()) { busy = false; button.setEnabled(true); status.setText(error.getMessage() == null ? "视频准备失败，请重试。" : error.getMessage()); } });
            }
        });
    }
    private void openWechat(Uri video) {
        if (isDestroyed()) return;
        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType("video/mp4");
        send.setPackage("com.tencent.mm");
        send.putExtra(Intent.EXTRA_STREAM, video);
        send.setClipData(ClipData.newRawUri("拾帧视频", video));
        send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivityForResult(send, SHARE);
            status.setText("已请求微信接收视频，请自行选择好友并确认。尚未确认发送成功。");
        } catch (RuntimeException error) {
            busy = false; button.setEnabled(true);
            status.setText("无法打开微信的视频分享：请确认已安装并登录微信；该微信版本也可能不支持此入口。");
        }
    }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
