package com.framekeep.sharetest;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.util.Base64;
import android.webkit.HttpAuthHandler;
import android.webkit.WebMessage;
import android.webkit.WebMessagePort;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.util.UUID;
import org.json.JSONObject;

public class WebActivity extends Activity {
    private WebView web;
    private TextView status;
    private volatile String trustedOrigin;
    private volatile boolean bundledUi;
    private int pageGeneration;
    private WebMessagePort port;
    private File partial;
    private FileOutputStream output;
    private long expected, received;
    private int sequence;
    private static final long LIMIT = 128L * 1024 * 1024;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setPadding(0,48,0,24);
        LinearLayout bar = new LinearLayout(this);
        EditText address = new EditText(this); address.setSingleLine(); address.setHint("输入你使用的拾帧网站地址");
        address.setText(getPreferences(0).getString("site", ""));
        bar.addView(address,new LinearLayout.LayoutParams(0,-2,1));
        Button open = new Button(this); open.setText("打开网站"); bar.addView(open); root.addView(bar);
        status = new TextView(this); status.setPadding(16,8,16,8); status.setText("在这里打开网站、登录并拾取视频。分享时不需要去相册选择。"); root.addView(status);
        web = new WebView(this); root.addView(web,new LinearLayout.LayoutParams(-1,0,1)); setContentView(root);
        web.getSettings().setJavaScriptEnabled(true); web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false); web.getSettings().setAllowContentAccess(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !origin(request.getUrl()).equals(trustedOrigin);
            }
            @Override public void onPageStarted(WebView view,String url,Bitmap icon) { pageGeneration++;disconnect(); }
            @Override public void onPageFinished(WebView view,String url) {
                if (!origin(Uri.parse(url)).equals(trustedOrigin)) return;
                if (!bundledUi) { probeOriginalLogin(pageGeneration);return; }
                String mainPath=Uri.parse(url).getPath();
                if (!("/".equals(mainPath)||"/index.html".equals(mainPath))) return;
                disconnect();
                WebMessagePort[] pair = web.createWebMessageChannel(); port = pair[0];
                final WebMessagePort current = port;
                port.setWebMessageCallback(new WebMessagePort.WebMessageCallback() {
                    @Override public void onMessage(WebMessagePort source,WebMessage message) {
                        if (source != port || !origin(Uri.parse(web.getUrl())).equals(trustedOrigin)) return;
                        receive(message.getData(),current);
                    }
                });
                web.postWebMessage(new WebMessage("framekeep-native-v1",new WebMessagePort[]{pair[1]}),Uri.parse(trustedOrigin));
                status.setText("已连接网页分享。请先拾取并下载视频，再点视频右下角的分享按钮。");
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request) {
                if (!bundledUi || !"GET".equals(request.getMethod()) || !origin(request.getUrl()).equals(trustedOrigin)) return null;
                String path = request.getUrl().getPath();
                if (path == null || path.equals("/") || path.equals("/index.html")) path = "/index.html";
                if (path.startsWith("/api/") || path.contains("..") || !path.matches("/[A-Za-z0-9_./-]+")) return null;
                String mime = path.endsWith(".html") ? "text/html" : path.endsWith(".js") ? "application/javascript" : path.endsWith(".css") ? "text/css" : path.endsWith(".svg") ? "image/svg+xml" : path.endsWith(".jpg") ? "image/jpeg" : path.endsWith(".woff2") ? "font/woff2" : "application/octet-stream";
                try { return new WebResourceResponse(mime,"UTF-8",getAssets().open("web"+path)); }
                catch (Exception missing) { return null; }
            }
            @Override public void onReceivedHttpAuthRequest(WebView view,HttpAuthHandler handler,String host,String realm) {
                if (!host.equalsIgnoreCase(Uri.parse(trustedOrigin).getHost()) || (web.getUrl()!=null && !origin(Uri.parse(web.getUrl())).equals(trustedOrigin))) {handler.cancel();return;}
                LinearLayout fields = new LinearLayout(WebActivity.this); fields.setOrientation(1);
                EditText user = new EditText(WebActivity.this); user.setHint("用户名（如网站需要）"); fields.addView(user);
                EditText pass = new EditText(WebActivity.this); pass.setHint("网站密码"); pass.setInputType(129); fields.addView(pass);
                new AlertDialog.Builder(WebActivity.this).setTitle("登录你输入的网站").setView(fields)
                    .setPositiveButton("登录",(d,w)->handler.proceed(user.getText().toString(),pass.getText().toString()))
                    .setNegativeButton("取消",(d,w)->handler.cancel()).setOnCancelListener(d->handler.cancel()).show();
            }
        });
        open.setOnClickListener(v->{
            String value=address.getText().toString().trim(); Uri uri=Uri.parse(value);
            if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) || uri.getHost()==null || uri.getUserInfo()!=null) { status.setText("请输入完整的网站地址，不要把密码放进网址。");return; }
            Runnable load=()->{pageGeneration++;disconnect();bundledUi=false;trustedOrigin=origin(uri);status.setText("请先在下方原网站输入访问密码，登录成功后自动进入分享界面。");getPreferences(0).edit().putString("site",value).apply();web.loadUrl(value);};
            if ("http".equals(uri.getScheme())) new AlertDialog.Builder(this).setTitle("这个网址没有加密")
                .setMessage("登录信息和视频可能被网络中的其他人看到。请确认这是你信任的测试网站；建议使用加密网址。")
                .setPositiveButton("继续打开",(d,w)->load.run()).setNegativeButton("取消",null).show();
            else load.run();
        });
    }
    private void probeOriginalLogin(int generation) {
        try(java.io.InputStream input=getAssets().open("login-probe.js")) {
            java.io.ByteArrayOutputStream source=new java.io.ByteArrayOutputStream();
            byte[] bytes=new byte[4096];int count;while((count=input.read(bytes))!=-1)source.write(bytes,0,count);
            web.evaluateJavascript(source.toString("UTF-8"),ignored->waitForLogin(generation,0));
        }catch(Exception error){status.setText("请完成网站登录后，再点“打开网站”重试。");}
    }
    private void waitForLogin(int generation,int attempt) {
        web.postDelayed(()->{
            if(isDestroyed()||generation!=pageGeneration||bundledUi||!origin(Uri.parse(web.getUrl())).equals(trustedOrigin))return;
            web.evaluateJavascript("window.__framekeepAuthState || 'pending'",value->{
                if(isDestroyed()||generation!=pageGeneration||bundledUi)return;
                if("\"ready\"".equals(value)) {
                    bundledUi=true;status.setText("登录已确认，正在打开带分享功能的界面…");web.loadUrl(trustedOrigin+"/");
                }else if("\"pending\"".equals(value)&&attempt<20)waitForLogin(generation,attempt+1);
                else status.setText("请在原网站输入访问密码。已登录但未切换时，可再点“打开网站”。");
            });
        },400);
    }
    private String origin(Uri uri) {
        if(uri==null||uri.getHost()==null||uri.getScheme()==null)return "";
        int p=uri.getPort();boolean defaultPort=p==-1||("https".equals(uri.getScheme())&&p==443)||("http".equals(uri.getScheme())&&p==80);
        return uri.getScheme().toLowerCase()+"://"+uri.getHost().toLowerCase()+(defaultPort?"":":"+p);
    }
    private void receive(String raw,WebMessagePort replyPort) {
        String id="";
        try {
            if(raw==null||raw.length()>50000)throw new IllegalArgumentException();
            JSONObject message=new JSONObject(raw);id=message.getString("id");if(id.length()>40)throw new IllegalArgumentException();
            String op=message.getString("op");
            if("begin".equals(op)) {
                abortTransfer();expected=message.getLong("size");
                if(expected<=0||expected>LIMIT||!"video/mp4".equals(message.getString("mime")))throw new IllegalArgumentException();
                File directory=new File(getCacheDir(),"shared");if(!directory.isDirectory()&&!directory.mkdirs())throw new IllegalStateException();
                partial=new File(directory,UUID.randomUUID().toString().replace("-","")+".mp4");
                output=new FileOutputStream(partial);received=0;sequence=0;
            } else if("chunk".equals(op)) {
                if(output==null||message.getInt("index")!=sequence)throw new IllegalArgumentException();
                String encoded=message.getString("data");if(!encoded.matches("[A-Za-z0-9+/]{1,43692}={0,2}"))throw new IllegalArgumentException();
                byte[] bytes=Base64.decode(encoded,Base64.NO_WRAP);
                if(bytes.length>32768||received+bytes.length>expected)throw new IllegalArgumentException();
                output.write(bytes);received+=bytes.length;sequence++;
            } else if("finish".equals(op)) {
                if(output==null||received!=expected)throw new IllegalArgumentException();
                output.close();output=null;Uri video=Uri.parse("content://com.framekeep.sharetest.video/"+partial.getName());partial=null;
                Intent send=new Intent(Intent.ACTION_SEND);send.setType("video/mp4");send.setPackage("com.tencent.mm");
                send.putExtra(Intent.EXTRA_STREAM,video);send.setClipData(ClipData.newRawUri("拾帧网页视频",video));send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivityForResult(send,11);status.setText("当前网页视频已交给微信，请自行选择好友并确认。");
            } else if("cancel".equals(op)) abortTransfer();
            else throw new IllegalArgumentException();
            replyPort.postMessage(new WebMessage(new JSONObject().put("id",id).put("ok",true).toString()));
        } catch(Exception error) {
            abortTransfer();
            try {replyPort.postMessage(new WebMessage(new JSONObject().put("id",id).put("ok",false).put("error","应用未能交接视频。请检查文件格式、完整性及微信是否可用。").toString()));}catch(Exception ignored){}
        }
    }
    private void abortTransfer(){try{if(output!=null)output.close();}catch(Exception ignored){}output=null;if(partial!=null)partial.delete();partial=null;}
    private void disconnect(){abortTransfer();if(port!=null)port.close();port=null;}
    @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override protected void onDestroy(){disconnect();web.destroy();super.onDestroy();}
}
